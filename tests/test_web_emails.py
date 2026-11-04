"""E-mails to check, and the e-mail lines on Tracker cards and the job page (made-up jobs and e-mails)."""

import pytest
from helpers import add_job, csrf, log_in, one
from test_tracker import version

from app import tracker

SAVE = """INSERT INTO emails (message_id, thread_id, received_on, sender_domain, label, labelled_by, job_id, to_check,
    subject, candidates) VALUES (?, 't', ?, 'greenhouse.io', ?, 'rules', ?, ?, ?, ?)"""


@pytest.fixture
def client(client):
    log_in(client)
    return client


@pytest.fixture
def jobs(conn):
    """Two Ready cards: Acme and Globex."""
    with conn:
        found = [
            add_job(conn, title, company=company)
            for company, title in (("Acme", "ML Intern"), ("Globex", "Data Scientist"))
        ]
        for job in found:
            version(conn, job)
    return found


def email(conn, message_id, label="interview", candidates="[]", job=None, to_check=1,
          subject="Your <b>application</b>", received="2026-11-04T10:00:00+00:00"):  # fmt: skip
    with conn:
        conn.execute(SAVE, (message_id, received, label, job, to_check, subject if to_check else None, candidates))


def post(client, address, **form):
    return client.post(address, data=form | {"csrf_token": csrf(client)}, follow_redirects=False)


def test_the_list_shows_each_e_mail_s_subject_as_text_and_its_likely_cards(client, conn, jobs):
    assert "Nothing to check." in client.get("/tracker/emails").text
    email(conn, "abc123", candidates=f"[{jobs[0]}]")
    text = client.get("/tracker/emails").text
    assert "Your &lt;b&gt;application&lt;/b&gt;" in text and "<b>application" not in text
    assert "This is Acme · ML Intern" in text and "Globex · Data Scientist" in text


def test_linking_to_a_card_moves_it_and_forgets_the_subject(client, conn, jobs):
    email(conn, "abc123", candidates=f"[{jobs[0]}]")
    reply = post(client, "/tracker/emails/abc123/link", job=str(jobs[0]), label="interview")
    assert reply.headers["location"] == "/tracker/emails?done=moved" and tracker.stage(conn, jobs[0]) == "interview"
    assert one(conn, "SELECT job_id, label, labelled_by, moved, to_check, subject, candidates FROM emails") == (
        jobs[0], "interview", "you", 1, 0, None, "[]"
    )  # fmt: skip
    assert "Nothing to check." in client.get("/tracker/emails?done=moved").text


def test_an_e_mail_can_be_linked_to_another_card_on_the_board(client, conn, jobs):
    email(conn, "abc123", label="unclear", candidates=f"[{jobs[0]}]")
    reply = post(client, "/tracker/emails/abc123/link", job=str(jobs[1]), label="other")
    assert reply.headers["location"] == "/tracker/emails?done=linked"
    assert "The card stays where it is" in client.get(reply.headers["location"]).text
    assert one(conn, "SELECT job_id, to_check FROM emails")[0:2] == (jobs[1], 0)


@pytest.mark.parametrize("job, label", [("999", "interview"), ("x", "interview"), ("", "interview"), (None, "offer!")])
def test_a_card_not_on_the_board_or_an_unknown_label_is_refused(client, conn, jobs, job, label):
    email(conn, "abc123")
    reply = post(client, "/tracker/emails/abc123/link", job=str(jobs[0]) if job is None else job, label=label)
    assert reply.status_code == 400 and one(conn, "SELECT to_check FROM emails")[0] == 1


@pytest.mark.parametrize("message_id", ["unknown", "decided", "a.b", "x" * 65])
def test_an_e_mail_not_on_the_list_is_not_found(client, conn, jobs, message_id):
    email(conn, "decided", to_check=0)
    assert post(client, f"/tracker/emails/{message_id}/dismiss").status_code == 404
    assert post(client, f"/tracker/emails/{message_id}/link", job=str(jobs[0]), label="offer").status_code == 404


def test_dismiss_takes_it_off_the_list_and_forgets_the_subject(client, conn, jobs):
    email(conn, "abc123")
    assert post(client, "/tracker/emails/abc123/dismiss").headers["location"] == "/tracker/emails?done=dismissed"
    assert one(conn, "SELECT to_check, subject, job_id FROM emails") == (0, None, None)


@pytest.mark.parametrize("action", ["link", "dismiss"])
def test_both_actions_need_the_page_s_token(client, conn, jobs, action):
    email(conn, "abc123")
    reply = client.post(f"/tracker/emails/abc123/{action}", data={"job": str(jobs[0]), "label": "offer"})
    assert reply.status_code == 403 and one(conn, "SELECT to_check FROM emails")[0] == 1


def test_the_tracker_links_to_the_list_and_a_card_shows_its_latest_e_mail(client, conn, jobs, phoenix):
    email(conn, "waiting")
    email(conn, "old", label="confirmation", job=jobs[0], to_check=0, received="2026-11-01T10:00:00+00:00")
    email(conn, "new", label="interview", job=jobs[0], to_check=0)
    text = client.get("/tracker").text
    assert '<a href="/tracker/emails">E-mails to check (1)</a>' in text and "E-mail 4 Nov · Interview" in text


@pytest.mark.parametrize("applied, opened", [("2026-11-03", True), ("x", False)])
def test_add_as_an_application_opens_the_form_on_the_e_mail_s_day(client, applied, opened):
    text = client.get(f"/tracker?applied={applied}").text
    assert ('id="add" open' in text) is opened and ('value="2026-11-03"' in text) is opened


def test_the_job_page_lists_the_job_s_e_mails(client, conn, jobs, phoenix):
    email(conn, "new", label="interview", job=jobs[0], to_check=0)
    text = client.get(f"/jobs/{jobs[0]}").text
    assert "2026-11-04 03:00 · Interview · greenhouse.io" in text and "Now in Ready." in text
