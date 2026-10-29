"""The Questionnaire page: login, the anti-forgery check, saving, a refused save and the download."""

from contextlib import closing

import pytest
from helpers import csrf, log_in

from app import db


@pytest.fixture
def client(client):
    log_in(client)
    return client


def save(client, **fields):
    return client.post("/questionnaire", data=fields | {"csrf_token": csrf(client)}, follow_redirects=False)


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
