# ruff: noqa: F811 (door, owner and paired are fixtures from test_web_plugin.py)
"""What the plugin says after a fill's answers went out: its re-reads, a pressed Submit and a tab that went away, and
"I didn't send it", with made-up jobs and forms. Nothing here opens a site."""

import json
import logging
from datetime import date
from pathlib import Path

import pytest
from helpers import add_job, csrf, log_in, one
from test_web_phone import ORIGIN, phone_login
from test_web_plugin import (
    BAD_KEYS,
    BAD_VALUES,
    FORM_ADDRESS,
    LEFT,
    LINK,
    LOG_OUT_ALL,
    OTHER_PLUGIN,
    REMOVE,
    SECOND_KEY,
    SETTINGS,
    VALUE,
    answered,
    door,  # noqa: F401
    fill_rows,
    held,
    log_out,
    ok,
    owner,  # noqa: F401
    packed,
    pair,
    paired,  # noqa: F401
    phone_dashboard,  # noqa: F401
    press,
    ready_job,
    refused,
    report,
    running,
    send,
)

from app.config import Config
from app.submit import Field, Form, fill_words, fills, preview
from app.submit import save as save_form
from app.web import actions, queries

QUESTIONS = tuple(Field(f"q{number}", f"Question {number}", "text", True, "company") for number in range(4))
HIDDEN = Field("utm", "Tracking", "hidden", False, "standard")
READ = "SELECT values_read, checked_on, clicked_on FROM fills ORDER BY id DESC"
RECORD = """INSERT INTO submissions (job_id, submitted_on, saved_on, how, address, fields, files)
    VALUES (?, '2026-11-05', 'then', 'by hand', ?, '[]', '[]')"""
ON_BOARD = "INSERT INTO tracker VALUES (?, 'ready', NULL, NULL, 'then')"
APPLIED = "INSERT INTO tracker VALUES (?, 'applied', NULL, '2026-10-17', 'then')"
MARKER = "fake-answer-only-in-the-read"
A_PERSON_S_FILE = "Fake-Person-Resume-2026.pdf"
LOOKS_SENT = "It looks sent. If the site took it, press I submitted it."
UNSENT_BUTTON = "/form/unsent"


def drawn(field):
    return {"key": field.key, "label": field.label, "kind": "text", "required": True}


def form_job(conn, *fields):
    """A job whose saved form has these questions, each answered."""
    with conn:
        job = add_job(conn, "ML Intern", apply_url=LINK)
    save_form(conn, job, Form(fields), "api")
    for field in fields:
        if field.kind != "hidden":
            preview.keep(conn, job, field, "Fake")
    return job


def answered_form(door, conn, data_dir, *fields):
    """A fill of this form that has had its answers; its job and ticket."""
    job = form_job(conn, *fields)
    ticket = running(door, conn, data_dir, job)
    assert send(door, ticket, [drawn(field) for field in fields if field.kind != "hidden"]).json()["go"]
    return job, ticket


def lines(**values):
    return [{"key": key, "value": value} for key, value in values.items()]


def kept(conn):
    found = one(conn, READ)[0]
    return found and json.loads(found)


def unsent(client, job):
    return client.post(f"/jobs/{job}/form/unsent", headers={"X-CSRF-Token": csrf(client)})


def box(client, job):
    return client.get(f"/jobs/{job}/form").text


def one_a_day(data_dir):
    return Config(data_dir=data_dir, fills_per_day=1, fill_pause_minutes=0)


# Re-reads


def test_a_read_is_kept_by_key_with_its_time(door, conn, data_dir, paired):
    ticket = answered(door, conn, data_dir)
    assert ok(report(door, ticket, "reread", values=[VALUE]))
    assert kept(conn) == {"first_name": "Fake Person"}
    assert one(conn, READ)[1] is not None and one(conn, READ)[2] is None
    assert fill_rows(conn)[-1][2:] == ("sent", None) and held(door)


def test_a_box_the_app_doesn_t_know_is_never_stored(door, conn, data_dir, paired):
    ticket = answered(door, conn, data_dir)
    assert ok(report(door, ticket, "reread", values=[VALUE, {"key": "new_box", "value": MARKER}]))
    assert kept(conn) == {"first_name": "Fake Person"}


@pytest.mark.parametrize("label", ["Portfolio password", "PASSWORD for the portfolio", "Your Password?"])
def test_a_question_about_a_password_is_never_stored(door, conn, data_dir, paired, label):
    secret = Field("secret", label, "text", False, "company")
    _, ticket = answered_form(door, conn, data_dir, QUESTIONS[0], secret)
    assert ok(report(door, ticket, "reread", values=lines(q0="Fake", secret=MARKER)))
    assert kept(conn) == {"q0": "Fake"}


def test_a_hidden_box_is_never_stored(door, conn, data_dir, paired):
    _, ticket = answered_form(door, conn, data_dir, QUESTIONS[0], HIDDEN)
    assert ok(report(door, ticket, "reread", values=lines(q0="Fake", utm=MARKER)))
    assert kept(conn) == {"q0": "Fake"}


def file_read(door, ticket, name):
    return report(door, ticket, "reread", values=[VALUE, {"key": "resume", "value": name}])


@pytest.mark.parametrize(
    ("name", "verdict"),
    [
        ("Resume.pdf", "same"),
        (A_PERSON_S_FILE, "changed"),
        ("Cover-letter.pdf", "changed"),
        ("", ""),
        (["x.pdf"], "changed"),
    ],
)
def test_a_file_box_keeps_only_a_verdict(door, conn, data_dir, paired, name, verdict):
    _, ticket, _ = packed(door, conn, data_dir)
    assert ok(file_read(door, ticket, name))
    assert kept(conn) == {"first_name": "Fake Person", "resume": verdict}


def test_a_person_s_file_name_never_reaches_the_row_or_the_ticket(door, conn, data_dir, paired):
    _, ticket, _ = packed(door, conn, data_dir)
    assert ok(file_read(door, ticket, A_PERSON_S_FILE))
    assert ok(report(door, ticket, "clicked", values=[{"key": "resume", "value": A_PERSON_S_FILE}, VALUE]))
    assert A_PERSON_S_FILE not in repr(conn.execute("SELECT * FROM fills").fetchall())
    assert A_PERSON_S_FILE not in repr(held(door))


def test_a_file_box_that_wasn_t_in_the_pack_is_not_kept(door, conn, data_dir, paired):
    _, ticket, _ = packed(door, conn, data_dir)
    letter = {"key": "cover_letter", "value": A_PERSON_S_FILE}
    assert ok(report(door, ticket, "reread", values=[VALUE, letter]))
    assert kept(conn) == {"first_name": "Fake Person"}


def test_a_file_verdict_counts_as_filled_only_when_it_says_something(door, conn, data_dir, paired):
    _, ticket, _ = packed(door, conn, data_dir)
    assert ok(report(door, ticket, "reread", values=[{"key": "resume", "value": ""}]))
    assert kept(conn) is None
    assert ok(report(door, ticket, "reread", values=[{"key": "resume", "value": A_PERSON_S_FILE}]))
    assert kept(conn) == {"resume": "changed"}


def test_only_unknown_boxes_is_an_empty_read(door, conn, data_dir, paired):
    ticket = answered(door, conn, data_dir)
    assert ok(report(door, ticket, "reread", values=[{"key": "new_box", "value": MARKER}]))
    assert one(conn, READ) == (None, None, None)


def test_control_characters_go_and_line_breaks_stay(door, conn, data_dir, paired):
    _, ticket = answered_form(door, conn, data_dir, *QUESTIONS[:2])
    assert ok(report(door, ticket, "reread", values=lines(q0="one\x00\nthree\x1b", q1=["a\x07", "b\nc"])))
    assert kept(conn) == {"q0": "one\nthree", "q1": ["a", "b\nc"]}


@pytest.mark.parametrize("empty", ["", "   ", "\n", "\x00", [], [""], ["  "]])
def test_a_read_with_nothing_filled_never_replaces(door, conn, data_dir, paired, empty):
    ticket = answered(door, conn, data_dir)
    assert ok(report(door, ticket, "reread", values=[VALUE]))
    before = one(conn, READ)
    assert ok(report(door, ticket, "reread", values=[VALUE | {"value": empty}]))
    assert ok(report(door, ticket, "reread", values=[]))
    assert one(conn, READ) == before


def test_a_first_read_with_nothing_filled_is_not_saved(door, conn, data_dir, paired):
    ticket = answered(door, conn, data_dir)
    assert ok(report(door, ticket, "reread", values=[VALUE | {"value": ""}]))
    assert one(conn, READ) == (None, None, None)


def all_four():
    return lines(q0="a", q1="b", q2="c", q3=["d"])


def test_a_form_reset_with_a_default_still_picked_doesn_t_replace(door, conn, data_dir, paired):
    _, ticket = answered_form(door, conn, data_dir, *QUESTIONS)
    assert ok(report(door, ticket, "reread", values=all_four()))
    assert ok(report(door, ticket, "reread", values=lines(q0="", q1="", q2="", q3=["default"])))
    assert kept(conn) == {"q0": "a", "q1": "b", "q2": "c", "q3": ["d"]}


def test_exactly_half_as_many_filled_replaces_the_whole_read(door, conn, data_dir, paired):
    _, ticket = answered_form(door, conn, data_dir, *QUESTIONS)
    assert ok(report(door, ticket, "reread", values=all_four()))
    assert ok(report(door, ticket, "reread", values=lines(q0="a2", q1="b2", q2="")))
    assert kept(conn) == {"q0": "a2", "q1": "b2", "q2": ""}  # q3 has gone: it reads as not read, never as its old value


def test_the_half_is_counted_from_the_last_read_kept(door, conn, data_dir, paired):
    _, ticket = answered_form(door, conn, data_dir, *QUESTIONS)
    assert ok(report(door, ticket, "reread", values=all_four()))
    assert ok(report(door, ticket, "reread", values=lines(q0="a", q1="b")))
    assert ok(report(door, ticket, "reread", values=lines(q0="a")))  # half of the 2 now held
    assert kept(conn) == {"q0": "a"}


@pytest.mark.parametrize("state", ["reread", "clicked"])
def test_a_read_before_the_answers_went_is_a_bad_request(door, conn, data_dir, paired, state):
    ticket = running(door, conn, data_dir)
    assert refused(report(door, ticket, state, values=[VALUE]), 400, "bad-request")
    assert one(conn, READ) == (None, None, None) and fill_rows(conn)[0][2:] == ("started", None)


@pytest.mark.parametrize("state", ["reread", "clicked", "gone"])
def test_a_kind_on_these_states_is_a_bad_request(door, conn, data_dir, paired, state):
    ticket = answered(door, conn, data_dir)
    assert refused(report(door, ticket, state, kind="limit", values=[VALUE]), 400, "bad-request")
    assert one(conn, READ) == (None, None, None) and held(door) and fill_rows(conn)[0][2:] == ("sent", None)


@pytest.mark.parametrize("state", ["reread", "clicked", "gone"])
def test_another_browser_s_ticket_is_no_ticket(door, conn, data_dir, paired, state):
    pair(conn, SECOND_KEY, "Desk", OTHER_PLUGIN)
    ticket = answered(door, conn, data_dir)
    assert refused(report(door, ticket, state, SECOND_KEY, OTHER_PLUGIN, values=[VALUE]), 404, "no-ticket")
    assert one(conn, READ) == (None, None, None) and held(door) and fill_rows(conn)[0][2:] == ("sent", None)


@pytest.mark.parametrize("state", ["reread", "clicked", "gone"])
@pytest.mark.parametrize(
    "body",
    [
        *({"values": [VALUE, VALUE | {"value": value}]} for value in BAD_VALUES),
        *({"values": [VALUE, VALUE | key]} for key in BAD_KEYS),
        {"values": None},
        {"left": [LEFT | {"why": "made-up"}]},
        {"left": None},
    ],
)
def test_one_bad_line_refuses_the_whole_body(door, conn, data_dir, paired, state, body):
    ticket = answered(door, conn, data_dir)
    assert refused(report(door, ticket, state, **body), 400, "bad-request")
    assert one(conn, READ) == (None, None, None) and held(door) and fill_rows(conn)[0][2:] == ("sent", None)


def test_a_read_after_the_report_still_counts_and_uses_no_report_up(door, conn, data_dir, paired):
    ticket = answered(door, conn, data_dir)
    assert ok(report(door, ticket, "reread", values=[VALUE]))
    assert ok(report(door, ticket, values=[VALUE]))  # the fill's own report still gets through
    assert fill_rows(conn)[0][2:] == ("filled", None)
    assert ok(report(door, ticket, "reread", values=[VALUE | {"value": "Changed"}]))
    assert kept(conn) == {"first_name": "Changed"}


@pytest.mark.parametrize("state", ["reread", "clicked"])
def test_a_busy_database_changes_nothing_at_a_read(door, conn, data_dir, paired, state):
    ticket = answered(door, conn, data_dir)
    assert ok(report(door, ticket, "reread", values=[VALUE]))
    before, ticket_held = one(conn, READ), next(iter(held(door).values()))
    conn.execute("BEGIN IMMEDIATE")
    response = report(door, ticket, state, values=[VALUE | {"value": "Changed"}])
    conn.rollback()
    assert refused(response, 503, "app-error")
    assert one(conn, READ) == before and ticket_held.read == {"first_name": "Fake Person"}
    assert ok(report(door, ticket, state, values=[VALUE | {"value": "Changed"}]))  # the next tick tries again
    assert kept(conn) == {"first_name": "Changed"} and ticket_held.read == {"first_name": "Changed"}


@pytest.mark.parametrize("state", ["reread", "clicked"])
def test_a_record_saved_while_the_ticket_lives_stops_the_reading(door, conn, data_dir, paired, state):
    job, ticket = with_a_read(door, conn, data_dir)
    with conn:
        conn.execute(RECORD, (job, FORM_ADDRESS))
    assert refused(report(door, ticket, state, values=[VALUE | {"value": MARKER}]), 404, "no-ticket")
    assert not held(door) and fill_rows(conn)[0][2:] == ("sent", "submitted")
    assert one(conn, READ)[:2] == (None, None)


def test_a_read_goes_nowhere_but_its_row(owner, door, conn, data_dir, paired, caplog):
    caplog.set_level(logging.DEBUG)
    job = ready_job(conn)
    ticket = answered(door, conn, data_dir, job)
    answers = [
        report(door, ticket, "reread", values=[VALUE | {"value": MARKER}]),
        report(door, ticket, "clicked", values=[VALUE | {"value": [MARKER, "x"]}]),
        report(door, ticket, "reread", values=[VALUE | {"value": MARKER}, {"key": 1, "value": MARKER}]),  # refused
    ]
    assert MARKER in one(conn, READ)[0]
    places = {
        "the answers": " ".join(answer.text for answer in answers),
        "the log": " ".join(record.getMessage() for record in caplog.records),
        "latest": repr(fill_words.latest(conn, job)),
        "recent": repr(fill_words.recent(conn)),
        "the box": box(owner, job),
        "Settings": owner.get(SETTINGS).text,
    }
    assert not [place for place, text in places.items() if MARKER in text]


def test_the_words_never_select_a_read():
    assert "values_read" not in Path(fill_words.__file__).read_text("utf-8")


# A pressed Submit


def test_a_press_keeps_the_read_and_the_ticket_and_records_nothing(owner, door, conn, data_dir, paired):
    job = ready_job(conn)
    ticket = answered(door, conn, data_dir, job)
    assert ok(report(door, ticket, "clicked", values=[VALUE]))
    assert kept(conn) == {"first_name": "Fake Person"} and one(conn, READ)[2] is not None
    assert held(door) and one(conn, "SELECT count(*) FROM submissions")[0] == 0
    assert LOOKS_SENT in box(owner, job)


def test_a_press_with_a_refused_read_still_counts(door, conn, data_dir, paired):
    _, ticket = answered_form(door, conn, data_dir, *QUESTIONS)
    assert ok(report(door, ticket, "reread", values=all_four()))
    assert ok(report(door, ticket, "clicked", values=lines(q0="", q1="", q2="", q3=["default"])))
    assert one(conn, READ)[2] is not None
    assert kept(conn) == {"q0": "a", "q1": "b", "q2": "c", "q3": ["d"]}


def test_a_press_with_nothing_read_yet_still_counts(door, conn, data_dir, paired):
    ticket = answered(door, conn, data_dir)
    assert ok(report(door, ticket, "clicked", values=[]))
    assert one(conn, READ)[:2] == (None, None) and one(conn, READ)[2] is not None


# The tab went away


def test_gone_before_the_answers_ends_the_fill(door, conn, data_dir, paired):
    ticket = running(door, conn, data_dir)
    assert ok(report(door, ticket, "gone"))
    assert fill_rows(conn)[0][2:] == ("ended", "gone") and not held(door)
    assert refused(report(door, ticket, "gone"), 404, "no-ticket")


def test_gone_keeps_the_read_and_records_nothing(door, conn, data_dir, paired):
    ticket = answered(door, conn, data_dir)
    assert ok(report(door, ticket, "reread", values=[VALUE]))
    assert ok(report(door, ticket, "gone"))
    assert fill_rows(conn)[0][2:] == ("sent", "gone") and not held(door)
    assert kept(conn) == {"first_name": "Fake Person"} and one(conn, "SELECT count(*) FROM submissions")[0] == 0


def test_gone_after_the_report_stays_filled(door, conn, data_dir, paired):
    ticket = answered(door, conn, data_dir)
    assert ok(report(door, ticket, values=[VALUE]))
    assert ok(report(door, ticket, "gone"))
    assert fill_rows(conn)[0][2:] == ("filled", "gone")


def test_a_busy_database_keeps_a_ticket_at_gone(door, conn, data_dir, paired):
    ticket = answered(door, conn, data_dir)
    conn.execute("BEGIN IMMEDIATE")
    response = report(door, ticket, "gone")
    conn.rollback()
    assert refused(response, 503, "app-error")
    assert held(door) and fill_rows(conn)[0][2:] == ("sent", None)


@pytest.mark.parametrize("state", ["reread", "clicked", "gone"])
def test_these_need_no_live_login(owner, door, conn, data_dir, paired, state):
    ticket = answered(door, conn, data_dir)
    log_out(owner)
    assert ok(report(door, ticket, state, values=[VALUE]))


# When a read is wiped


def with_a_read(door, conn, data_dir, job=None):
    job = job or ready_job(conn)
    ticket = answered(door, conn, data_dir, job)
    assert ok(report(door, ticket, "reread", values=[VALUE]))
    assert kept(conn)
    return job, ticket


def test_removing_the_browser_wipes_the_read(owner, door, conn, data_dir, paired):
    with_a_read(door, conn, data_dir)
    owner.post(REMOVE, data={"id": str(paired), "csrf_token": csrf(owner)})
    assert fill_rows(conn)[0][2:] == ("sent", "browser-removed") and one(conn, READ)[:2] == (None, None)


def test_log_out_all_wipes_the_read(owner, door, conn, data_dir, paired):
    with_a_read(door, conn, data_dir)
    owner.post(LOG_OUT_ALL, data={"csrf_token": csrf(owner)}, follow_redirects=False)
    assert fill_rows(conn)[0][2:] == ("sent", "browser-removed")
    assert one(conn, "SELECT values_read FROM fills")[0] is None


def test_removing_any_browser_wipes_a_read_kept_at_gone(owner, door, conn, data_dir, paired):
    other = pair(conn, SECOND_KEY, "Desk", OTHER_PLUGIN)
    _, ticket = with_a_read(door, conn, data_dir)
    assert ok(report(door, ticket, "gone"))
    owner.post(REMOVE, data={"id": str(other), "csrf_token": csrf(owner)})
    assert one(conn, READ)[:2] == (None, None) and fill_rows(conn)[0][2:] == ("sent", "gone")


def test_log_out_all_wipes_a_read_kept_at_gone(owner, door, conn, data_dir, paired):
    _, ticket = with_a_read(door, conn, data_dir)
    assert ok(report(door, ticket, "gone"))
    owner.post(LOG_OUT_ALL, data={"csrf_token": csrf(owner)}, follow_redirects=False)
    assert one(conn, READ)[:2] == (None, None) and fill_rows(conn)[0][2:] == ("sent", "gone")


def test_removing_a_browser_wipes_a_read_whose_ticket_ran_out(owner, door, conn, data_dir, paired, clock):
    with_a_read(door, conn, data_dir)
    clock.now += fills.RUNNING_SECONDS + 1
    log_in(owner)
    owner.post(REMOVE, data={"id": str(paired), "csrf_token": csrf(owner)})
    assert one(conn, READ)[:2] == (None, None)


def test_a_fill_still_running_after_a_remove_starts_its_read_again(owner, door, conn, data_dir, paired):
    other = pair(conn, SECOND_KEY, "Desk", OTHER_PLUGIN)
    _, ticket = answered_form(door, conn, data_dir, *QUESTIONS)
    assert ok(report(door, ticket, "reread", values=all_four()))
    owner.post(REMOVE, data={"id": str(other), "csrf_token": csrf(owner)})
    assert kept(conn) is None and next(iter(held(door).values())).read == {}
    assert ok(report(door, ticket, "reread", values=lines(q0="a")))  # nothing is held, so the half rule asks for 1
    assert kept(conn) == {"q0": "a"}


def test_a_busy_database_wipes_nothing_at_a_remove(owner, door, conn, data_dir, paired):
    other = pair(conn, SECOND_KEY, "Desk", OTHER_PLUGIN)
    _, ticket = with_a_read(door, conn, data_dir)
    assert ok(report(door, ticket, "gone"))
    conn.execute("BEGIN IMMEDIATE")
    owner.post(REMOVE, data={"id": str(other), "csrf_token": csrf(owner)})
    conn.rollback()
    assert kept(conn) and one(conn, "SELECT count(*) FROM paired_browsers")[0] == 2


def test_a_replacing_press_wipes_the_read(door, conn, data_dir, paired):
    job, _ = with_a_read(door, conn, data_dir)
    press(door, conn, data_dir, job)
    assert [row[2:] for row in fill_rows(conn)] == [("sent", "replaced"), ("waiting", None)]
    assert one(conn, "SELECT count(values_read), count(checked_on) FROM fills") == (0, 0)


def test_a_new_press_wipes_a_read_whose_ticket_ran_out(door, conn, data_dir, paired, clock):
    job, _ = with_a_read(door, conn, data_dir)
    clock.now += fills.RUNNING_SECONDS + 1
    press(door, conn, data_dir, job)
    assert one(conn, "SELECT count(values_read) FROM fills")[0] == 0


def test_a_new_press_wipes_a_read_kept_at_gone(door, conn, data_dir, paired):
    job, ticket = with_a_read(door, conn, data_dir)
    assert ok(report(door, ticket, "gone"))
    press(door, conn, data_dir, job)
    assert one(conn, "SELECT count(values_read) FROM fills")[0] == 0


def test_a_refused_press_wipes_nothing(door, conn, data_dir, paired):
    job, _ = with_a_read(door, conn, data_dir)
    found = preview.preview(conn, data_dir, job)
    used = door.app.state.tickets.make(conn, queries.job(conn, job), found, one_a_day(data_dir))
    assert used and kept(conn)


# I didn't send it


def test_i_didn_t_send_it_ends_the_fill_and_wipes_the_read(owner, door, conn, data_dir, paired):
    job, ticket = with_a_read(door, conn, data_dir)
    assert ok(report(door, ticket, "clicked", values=[VALUE]))
    with conn:
        conn.execute(ON_BOARD, (job,))
    card = conn.execute("SELECT * FROM tracker").fetchall()
    page = unsent(owner, job)
    assert page.status_code == 200 and "you said you didn" in page.text
    assert fill_rows(conn)[0][2:] == ("sent", "not-sent") and kept(conn) is None and not held(door)
    assert one(conn, "SELECT count(*) FROM submissions")[0] == 0
    assert conn.execute("SELECT * FROM tracker").fetchall() == card
    assert refused(report(door, ticket, "reread", values=[VALUE]), 404, "no-ticket")
    assert LOOKS_SENT not in page.text and UNSENT_BUTTON not in page.text


def test_the_time_goes_with_the_read(owner, door, conn, data_dir, paired):
    job, _ = with_a_read(door, conn, data_dir)
    assert "Last read of the form" in box(owner, job)
    unsent(owner, job)
    assert one(conn, READ)[:2] == (None, None) and fill_words.latest(conn, job)["read_at"] is None
    assert "Last read of the form" not in box(owner, job)


def test_it_works_with_no_ticket(owner, door, conn, data_dir, paired):
    job, ticket = with_a_read(door, conn, data_dir)
    assert ok(report(door, ticket, "gone"))
    assert unsent(owner, job).status_code == 200
    assert fill_rows(conn)[0][2:] == ("sent", "not-sent") and kept(conn) is None


def test_it_closes_a_filled_fill(owner, door, conn, data_dir, paired):
    job = ready_job(conn)
    ticket = answered(door, conn, data_dir, job)
    assert ok(report(door, ticket, values=[VALUE]))
    unsent(owner, job)
    assert fill_rows(conn)[0][2:] == ("filled", "not-sent")


def test_it_leaves_a_newer_waiting_press_alone(owner, door, conn, data_dir, paired):
    job, ticket = with_a_read(door, conn, data_dir)
    assert ok(report(door, ticket, "gone"))
    press(door, conn, data_dir, job)
    waiting = next(iter(held(door).values()))
    unsent(owner, job)
    assert [row[2:] for row in fill_rows(conn)] == [("sent", "gone"), ("waiting", None)]
    assert held(door)[job] is waiting


@pytest.mark.parametrize("state", ["waiting", "started", "stopped", "ended"])
def test_it_only_closes_a_fill_whose_answers_went(owner, door, conn, data_dir, paired, state):
    job = ready_job(conn)
    with conn:
        conn.execute(
            "INSERT INTO fills (job_id, address, started_on, toolbar, state) VALUES (?, ?, ?, 0, ?)",
            (job, FORM_ADDRESS, fills.now(), state),
        )
    assert UNSENT_BUTTON not in box(owner, job)
    unsent(owner, job)
    assert fill_rows(conn)[0][2:] == (state, None)


def test_it_doesn_t_give_the_fill_back(owner, door, conn, data_dir, paired):
    job, _ = with_a_read(door, conn, data_dir)
    unsent(owner, job)
    found = preview.preview(conn, data_dir, job)
    assert "Today's 1 fill is used" in fills.why_not(conn, queries.job(conn, job), found, one_a_day(data_dir))


def test_a_job_with_a_record_changes_nothing(owner, door, conn, data_dir, paired):
    job, ticket = with_a_read(door, conn, data_dir)
    with conn:
        conn.execute(APPLIED, (job,))
    assert ok(report(door, ticket, "gone"))
    data = {"on": date.today().isoformat(), "csrf_token": csrf(owner)}
    assert owner.post(f"/jobs/{job}/form/submitted", data=data).status_code == 200
    before = fill_rows(conn)
    assert unsent(owner, job).status_code == 200
    assert fill_rows(conn) == before and UNSENT_BUTTON not in box(owner, job)


def test_a_busy_database_says_so_and_changes_nothing(owner, door, conn, data_dir, paired):
    job, _ = with_a_read(door, conn, data_dir)
    conn.execute("BEGIN IMMEDIATE")
    page = unsent(owner, job)
    conn.rollback()
    assert actions.BUSY in page.text and held(door)
    assert fill_rows(conn)[0][2:] == ("sent", None) and kept(conn)


def test_it_works_on_the_phone(phone_dashboard, conn):
    job = ready_job(conn)
    past(conn, job)
    _, phone = phone_dashboard
    cookie, session = phone_login(phone)
    answer = phone.post(f"/jobs/{job}/form/unsent", headers=cookie | ORIGIN | {"X-CSRF-Token": session.csrf})
    assert answer.status_code == 200 and fill_rows(conn)[0][2:] == ("sent", "not-sent")


# What the box and Settings say


def past(conn, job, state="sent", stop=None, started="2026-11-05T21:02:00+00:00", **values):
    add = "INSERT INTO fills (job_id, address, started_on, toolbar, state, stop_kind) VALUES (?, ?, ?, 0, ?, ?)"
    with conn:
        row = conn.execute(add, (job, FORM_ADDRESS, started, state, stop)).lastrowid
        for name, value in values.items():
            conn.execute(f"UPDATE fills SET {name} = ? WHERE id = ?", (value, row))  # noqa: S608 (fixed test names)


@pytest.mark.parametrize(
    ("state", "stop", "clicked", "looks_sent", "out"),
    [
        ("sent", None, None, False, True),
        ("filled", None, None, False, True),
        ("sent", "gone", None, False, True),
        ("sent", "bot-check", None, False, True),
        ("filled", None, "2026-11-05T21:05:00+00:00", True, True),
        ("sent", "gone", "2026-11-05T21:05:00+00:00", True, True),
        ("sent", "not-sent", "2026-11-05T21:05:00+00:00", False, False),
        ("filled", "not-sent", None, False, False),
        ("started", None, None, False, False),
        ("waiting", None, None, False, False),
        ("stopped", "limit", None, False, False),
        ("ended", "gone", None, False, False),
    ],
)
def test_the_box_is_told_what_it_may_offer(owner, conn, paired, state, stop, clicked, looks_sent, out):
    job = ready_job(conn)
    past(conn, job, state, stop, clicked_on=clicked)
    latest = fill_words.latest(conn, job)
    assert (latest["looks_sent"], latest["out"]) == (looks_sent, out)
    (recent,) = fill_words.recent(conn)
    assert (recent["looks_sent"], recent["out"]) == (looks_sent, out)
    page = box(owner, job)
    assert (LOOKS_SENT in page, UNSENT_BUTTON in page) == (looks_sent, out)


def test_only_the_newest_row_decides(owner, conn, paired):
    job = ready_job(conn)
    past(conn, job, "filled", clicked_on="2026-11-05T21:05:00+00:00")
    past(conn, job, "waiting")
    page = box(owner, job)
    assert LOOKS_SENT not in page and UNSENT_BUTTON not in page


def test_times_read_as_the_laptop_s_clock(owner, conn, paired, phoenix):
    job = ready_job(conn)
    past(conn, job, taken_on="2026-11-05T21:30:00+00:00", checked_on="2026-11-05T21:45:00+00:00")
    latest = fill_words.latest(conn, job)
    assert (latest["taken"], latest["read_at"]) == ("5 Nov 14:30", "5 Nov 14:45")
    page = box(owner, job)
    assert "opened 5 Nov 14:30" in page and "Last read of the form: 5 Nov 14:45." in page
    assert "opened 5 Nov 14:30" in owner.get(SETTINGS).text


def test_nothing_is_said_of_times_a_row_doesn_t_have(owner, conn, paired):
    job = ready_job(conn)
    past(conn, job)
    latest = fill_words.latest(conn, job)
    assert latest["taken"] is None and latest["read_at"] is None
    page = box(owner, job)
    assert "opened " not in page and "Last read of the form" not in page


def test_opened_in_the_same_minute_is_not_said_twice(owner, conn, paired):
    job = ready_job(conn)
    past(conn, job, started="2026-11-05T21:02:00+00:00", taken_on="2026-11-05T21:02:30+00:00")
    assert "opened " not in box(owner, job) and "opened " not in owner.get(SETTINGS).text


def test_the_buttons_stay_out_once_a_record_is_saved(owner, door, conn, data_dir, paired):
    job = ready_job(conn)
    ticket = answered(door, conn, data_dir, job)
    assert ok(report(door, ticket, "clicked", values=[VALUE]))
    with conn:
        conn.execute(APPLIED, (job,))
    owner.post(f"/jobs/{job}/form/submitted", data={"on": date.today().isoformat(), "csrf_token": csrf(owner)})
    page = box(owner, job)
    assert LOOKS_SENT not in page and UNSENT_BUTTON not in page
