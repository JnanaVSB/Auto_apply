"""The Questionnaire page: login, the anti-forgery check, saving, a refused save and the download."""

import re
from contextlib import closing

import pytest
from helpers import csrf, log_in

from app import db, questionnaire


@pytest.fixture
def client(client):
    log_in(client)
    return client


def opened(client):
    """The page's mark of when it was opened, as a browser would send it back."""
    return re.search(r'name="saved_on" value="([^"]*)"', client.get("/questionnaire").text)[1]


def save(client, **fields):
    fields = {"saved_on": opened(client), "csrf_token": csrf(client)} | fields
    return client.post("/questionnaire", data=fields, follow_redirects=False)


def test_the_page_needs_a_login(client):
    client.post("/logout", data={"csrf_token": csrf(client)})
    assert client.get("/questionnaire", follow_redirects=False).headers["location"] == "/login"
    assert client.get("/questionnaire/download", follow_redirects=False).headers["location"] == "/login"


def test_saving_needs_the_pages_token(client, data_dir):
    answer = client.post("/questionnaire", data={"contact.full_name": "Alex Example", "csrf_token": "wrong"})
    assert answer.status_code == 403
    with closing(db.connect(data_dir)) as conn:
        assert conn.execute("SELECT count(*) FROM questionnaire").fetchone()[0] == 0


def test_saved_answers_come_back_on_the_page(client):
    answer = save(client, **{"contact.full_name": "Alex Example", "availability.remote": "1"})
    assert answer.status_code == 303 and answer.headers["location"] == "/questionnaire?saved=1"
    text = client.get("/questionnaire?saved=1").text
    assert 'value="Alex Example"' in text and "Saved." in text
    assert 'name="availability.remote" type="checkbox" value="1" checked' in text
    assert "<option selected>I prefer not to answer</option>" in text
    assert "data-unsaved" in text and '<script src="/static/unsaved.js" defer>' in text


def test_a_refused_save_keeps_what_was_typed_and_saves_nothing(client, data_dir):
    answer = save(client, **{"contact.full_name": "Alex <b>Example</b>", "contact.phone": "12"})
    assert answer.status_code == 200
    assert "Not saved: Contact, phone: needs 7 to 15 digits" in answer.text
    assert 'value="Alex &lt;b&gt;Example&lt;/b&gt;"' in answer.text  # shown as typed, escaped
    with closing(db.connect(data_dir)) as conn:
        assert conn.execute("SELECT count(*) FROM questionnaire").fetchone()[0] == 0


def test_the_download_is_a_private_file(client):
    save(client, **{"contact.email": "alex@example.com"})
    answer = client.get("/questionnaire/download")
    assert answer.headers["content-disposition"] == 'attachment; filename="questionnaire.yaml"'
    assert answer.headers["cache-control"] == "no-store"
    assert "email: alex@example.com" in answer.text


def test_questions_a_form_asked_come_first_with_who_asked_and_the_consents_are_there(client, data_dir):
    with closing(db.connect(data_dir)) as conn:
        questionnaire.save(conn, questionnaire.check({"more": [
            {"question": "Why this company?", "answer": "Made-up answer."},
            {"question": "What is your shoe size?", "asked_by": "<b>Acme</b>", "asked_on": "2026-11-04"},
            {"question": "Do you speak Esperanto?", "asked_by": "Globex"},
        ]}))  # fmt: skip
    text = client.get("/questionnaire").text
    first, second, answered = (text.index(question) for question in (
        "What is your shoe size?", "Do you speak Esperanto?", "Why this company?"
    ))  # fmt: skip
    assert first < second < answered
    assert '<span class="mark warn">needs answer</span>' in text
    assert "asked by &lt;b&gt;Acme&lt;/b&gt;, 4 Nov</span>" in text and "asked by Globex</span>" in text
    assert "asked_by" not in text  # kept on the server: the page sends questions and answers only
    assert '<option value="ask" selected>Ask me each time</option>' in text
    assert "AI-use promises and arbitration agreements are always asked" in text
    assert "(up to 300)" in text


def rows(count, length=1000):
    """`count` full More answers rows, as the page sends them."""
    return {
        name: value
        for number in range(count)
        for name, value in (
            (f"more.question.{number}", f"{number} " + "q" * length),
            (f"more.answer.{number}", "a" * 2000),
        )
    }


def test_300_full_rows_save_through_the_page_and_rows_can_be_deleted_again(client, data_dir):
    assert save(client, **rows(300, 996)).status_code == 303
    with closing(db.connect(data_dir)) as conn:
        assert len(questionnaire.load(conn)[0]["more"]) == 300
    assert save(client, **rows(5, 996)).status_code == 303
    with closing(db.connect(data_dir)) as conn:
        assert len(questionnaire.load(conn)[0]["more"]) == 5


def test_a_301st_row_is_refused_naming_the_section(client, data_dir):
    answer = save(client, **rows(301, 20))
    assert answer.status_code == 200 and "Not saved: More answers: List should have at most 300 items" in answer.text
    with closing(db.connect(data_dir)) as conn:
        assert conn.execute("SELECT count(*) FROM questionnaire").fetchone()[0] == 0


def test_who_asked_stays_after_a_save_through_the_page(client, data_dir):
    with closing(db.connect(data_dir)) as conn:
        questionnaire.remember(conn, "What is your shoe size?", "Acme")
    assert save(client, **{"more.question.0": "What is your shoe size?", "more.answer.0": "9"}).status_code == 303
    with closing(db.connect(data_dir)) as conn:
        assert [(row["answer"], row["asked_by"]) for row in questionnaire.load(conn)[0]["more"]] == [("9", "Acme")]


def test_a_page_opened_before_the_job_box_added_a_row_drops_nothing(client, data_dir):
    stale = opened(client)
    with closing(db.connect(data_dir)) as conn:
        questionnaire.remember(conn, "What is your shoe size?", "Acme")
    answer = save(client, saved_on=stale, **{"contact.full_name": "Alex Example"})
    assert answer.status_code == 200 and "This page is out of date: reload it" in answer.text
    assert 'value="Alex Example"' in answer.text  # what you typed stays on the page
    with closing(db.connect(data_dir)) as conn:
        values = questionnaire.load(conn)[0]
    assert [row["question"] for row in values["more"]] == ["What is your shoe size?"]
    assert values["contact"]["full_name"] == ""


def test_a_browsers_new_lines_save_in_both_text_areas(client, data_dir):
    typed = {"availability.locations": "Austin\r\nBoston", "more.question.0": "Hobby?", "more.answer.0": "Chess\r\nGo"}
    assert save(client, **typed).status_code == 303
    with closing(db.connect(data_dir)) as conn:
        values = questionnaire.load(conn)[0]
    assert (values["availability"]["locations"], values["more"][0]["answer"]) == ("Austin\nBoston", "Chess\nGo")


def test_a_refused_save_can_be_corrected_and_saved_from_the_same_page(client):
    stale = opened(client)
    assert "Not saved: Contact, phone" in save(client, saved_on=stale, **{"contact.phone": "12"}).text
    assert save(client, saved_on=stale, **{"contact.phone": "5551234567"}).status_code == 303
