"""The record of an application made by hand: what the app prepared, kept once with the card's move to Applied, with
a made-up person and job. The app never submits anything."""

import hashlib
from datetime import date

import pytest
from helpers import add_job, one

from app import questionnaire, tracker
from app.documents import pdf
from app.submit import Choice, Field, match, record
from app.submit.record import Refused

DAY = date(2026, 11, 4)
DONE = """INSERT INTO preparations (job_id, kind, version, sources, writer, status, draft, created_on)
    VALUES (?, ?, ?, '[]', '{}', 'done', '{}', 'now')"""
EMAIL = Field("email", "Email", "text", True, "standard")
TEAM = Field("question_1", "Which team?", "single", True, "company", choices=(Choice("Search", "8"),))
HOBBY = Field("question_2", "Hobby?", "text", False, "company")
WHY = Field("question_3", "Why Acme?", "long_text", True, "company")
AI = Field("question_4", "I consent to AI tools", "consent", True, "privacy")
TOKEN = Field("token", "token", "hidden", False, "standard")
LINES = (
    match.Line(EMAIL, "fill", "sam@example.com", "Questionnaire: Contact"),
    match.Line(TEAM, "fill", ("8",), "your answer for this job"), match.Line(HOBBY, "empty"),
    match.Line(WHY, "write"), match.Line(AI, "left", note="AI use"),
    match.Line(TEAM, "decide", decision="unknown"), match.Line(TOKEN, "hidden"),
)  # fmt: skip
ANSWERS = {
    "contact": {"full_name": "Sam Lee Park", "email": "sam@example.com", "city": "Austin", "country": "United States"},
    "work": {"authorized_us": "yes", "sponsorship_now": "no"},
    "availability": {"earliest_start": "2027-01-15"},
    "salary": {"minimum": 120_000, "maximum": 150_000},
}


def made_up(conn, data_dir, letter=False, pdfs=True):
    """A made-up job with a finished resume version 3 (its card is in Ready), and its PDF."""
    with conn:
        job = add_job(conn, "ML Intern")
        conn.execute(DONE, (job, "resume", 3))
        if letter:
            conn.execute(DONE, (job, "cover_letter", 1))
    if pdfs:
        target = pdf.tailored(data_dir, job, 3)
        target.parent.mkdir(parents=True)
        target.write_bytes(b"%PDF-1.7 fake")
    return {"id": job, "apply_url": "https://jobs.example/acme/1"}


def test_the_copy_panel_holds_only_the_answers_you_gave():
    assert record.standard(questionnaire.check(ANSWERS).model_dump(mode="json")) == [
        ("Name", "Sam Lee Park"), ("E-mail", "sam@example.com"), ("Location", "Austin, United States"),
        ("Work authorization", "Yes"), ("Sponsorship now", "No"), ("Earliest start", "January 15, 2027"),
        ("Salary", "120,000-150,000 USD per year"),
    ]  # fmt: skip


def test_a_record_keeps_what_was_prepared_and_marks_what_you_did_yourself(conn, data_dir):
    job = made_up(conn, data_dir)
    record.submit(conn, data_dir, job, DAY, LINES)
    found = record.recorded(conn, job["id"])
    assert (found["submitted_on"], found["how"], found["address"]) == ("2026-11-04", "by hand", job["apply_url"])
    assert found["fields"] == [
        {"question": "Email", "value": "sam@example.com", "from": "Questionnaire: Contact", "note": ""},
        {"question": "Which team?", "value": "Search", "from": "your answer for this job", "note": ""},
        {"question": "Hobby?", "value": "", "from": "left empty", "note": ""},
        {"question": "Why Acme?", "value": "", "from": "answered by you on the site", "note": ""},
        {"question": "I consent to AI tools", "value": "", "from": "answered by you on the site", "note": "AI use"},
        {"question": "Which team?", "value": "", "from": "answered by you on the site", "note": ""},
    ]


def test_without_a_read_form_the_record_keeps_your_standard_answers(conn, data_dir):
    job = made_up(conn, data_dir)
    questionnaire.save(conn, questionnaire.check(ANSWERS))
    record.submit(conn, data_dir, job, DAY, None)
    assert record.recorded(conn, job["id"])["fields"][:2] == [
        {"question": "Name", "value": "Sam Lee Park", "from": "Questionnaire"},
        {"question": "E-mail", "value": "sam@example.com", "from": "Questionnaire"},
    ]


def test_the_card_moves_to_applied_as_yours_on_the_chosen_date(conn, data_dir):
    job = made_up(conn, data_dir)
    record.submit(conn, data_dir, job, DAY, LINES)
    assert one(conn, "SELECT stage, applied_on FROM tracker WHERE job_id = ?", job["id"]) == ("applied", "2026-11-04")
    assert [(move["to_stage"], move["moved_by"]) for move in tracker.history(conn, job["id"])] == [("applied", "you")]


def test_a_files_hash_is_its_sha256_and_a_missing_pdf_is_noted_and_never_stops_the_record(conn, data_dir):
    job = made_up(conn, data_dir, letter=True)
    record.submit(conn, data_dir, job, DAY, LINES)
    assert record.recorded(conn, job["id"])["files"] == [
        {"slot": "resume", "label": "Resume v3", "version": 3, "sha256": hashlib.sha256(b"%PDF-1.7 fake").hexdigest(),
         "note": ""},
        {"slot": "cover_letter", "label": "Cover letter v1", "version": 1, "sha256": "", "note": "file was missing"},
    ]  # fmt: skip


def test_a_card_moved_by_another_tab_leaves_no_record(conn, data_dir, monkeypatch):
    job, stages = made_up(conn, data_dir), iter([None, "interview"])  # in Ready at the check, gone at the move
    monkeypatch.setattr(record.tracker, "stage", lambda *_: next(stages))
    monkeypatch.setattr(record.tracker, "moved", lambda *_: False)
    with pytest.raises(Refused, match="The card moved meanwhile: it is in Interview now. Nothing was recorded"):
        record.submit(conn, data_dir, job, DAY, LINES)
    assert one(conn, "SELECT count(*) FROM submissions")[0] == 0


def test_a_second_record_is_refused_calmly(conn, data_dir):
    job = made_up(conn, data_dir)
    record.submit(conn, data_dir, job, DAY, LINES)
    with pytest.raises(Refused, match="Already recorded on 2026-11-04"):
        record.submit(conn, data_dir, job, date(2026, 11, 5), LINES)
    assert one(conn, "SELECT count(*) FROM submissions")[0] == 1


def test_a_record_saved_by_a_second_dashboard_meanwhile_is_refused_calmly(conn, data_dir, monkeypatch):
    job = made_up(conn, data_dir)
    with conn:
        conn.execute(
            "INSERT INTO submissions (job_id, submitted_on, saved_on, how, address, fields, files) VALUES "
            "(?, '2026-11-03', 'now', 'by hand', 'https://jobs.example/1', '[]', '[]')", (job["id"],),
        )  # fmt: skip
    real, first = record.recorded, iter([None])  # none yet at the check; there at the save
    monkeypatch.setattr(record, "recorded", lambda *found: next(first, real(*found)))
    with pytest.raises(Refused, match="Already recorded on 2026-11-03"):
        record.submit(conn, data_dir, job, DAY, LINES)
    assert one(conn, "SELECT count(*) FROM submissions")[0] == 1 and tracker.stage(conn, job["id"]) is None


def test_no_card_or_no_finished_resume_says_what_to_do(conn, data_dir):
    with conn:
        job = add_job(conn, "ML Intern")
    with pytest.raises(Refused, match="use Add an application on the Tracker page, or press Prepare first"):
        record.submit(conn, data_dir, {"id": job, "apply_url": "x"}, DAY, LINES)
    tracker.move(conn, job, "ready", "applied", DAY)  # a card, but no finished resume
    with pytest.raises(Refused, match="press Prepare first"):
        record.submit(conn, data_dir, {"id": job, "apply_url": "x"}, DAY, LINES)


def test_a_card_past_ready_is_left_where_it_is(conn, data_dir):
    job = made_up(conn, data_dir)
    tracker.move(conn, job["id"], "ready", "applied", DAY)
    with pytest.raises(Refused, match="This job is already in Applied: nothing changed"):
        record.submit(conn, data_dir, job, DAY, LINES)
    assert record.recorded(conn, job["id"]) is None


@pytest.mark.parametrize("day", [date(1999, 12, 31), date(2999, 1, 1)])
def test_a_date_outside_the_trackers_rule_is_refused(conn, data_dir, day):
    with pytest.raises(Refused, match="Choose the date you applied"):
        record.submit(conn, data_dir, made_up(conn, data_dir), day, LINES)
