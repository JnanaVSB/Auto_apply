# ruff: noqa: F811 (door, owner and paired are fixtures from test_web_plugin.py)
"""The thank-you switch: a record the app makes by itself, the one write that can't be erased. All three sites ship off,
so each test here switches one on by hand. Made-up jobs and forms; nothing here opens a site."""

from dataclasses import replace
from datetime import date

import pytest
from helpers import csrf, log_in, one
from test_web_plugin import (
    BAD_KEYS,
    BAD_VALUES,
    LEFT,
    OTHER_PLUGIN,
    SECOND_KEY,
    START,
    VALUE,
    answered,
    call,
    door,  # noqa: F401
    fill_rows,
    held,
    log_out,
    ok,
    owner,  # noqa: F401
    pair,
    paired,  # noqa: F401
    paper,
    press,
    ready_job,
    refused,
    report,
    running,
)

from app.submit import Thanks, ashby, fill_words, fills, greenhouse, lever, record

CARD = "INSERT INTO tracker VALUES (?, 'ready', NULL, NULL, 'then')"
APPLIED = "INSERT INTO tracker VALUES (?, 'applied', NULL, '2026-10-17', 'then')"
LOOKS_SENT = "It looks sent. If the site took it, press I submitted it."
SIGN = Thanks(path="/thanks")
SAID = "your press of Submit was followed by the site's thank-you page, so it was recorded"


@pytest.fixture
def site_on(monkeypatch):
    monkeypatch.setattr(greenhouse, "DRAWN", replace(greenhouse.DRAWN, thanks=SIGN))


def on_board(conn, data_dir, card=CARD):
    """A job with a card and a finished resume; a fill of it has its answers out next."""
    job = ready_job(conn)
    paper(conn, data_dir, job)
    with conn:
        conn.execute(card, (job,))
    return job


def pressed(door, conn, data_dir, card=CARD):
    """A fill whose answers went, read, then your press of Submit; its job and ticket."""
    job = on_board(conn, data_dir, card)
    ticket = answered(door, conn, data_dir, job)
    assert ok(report(door, ticket, "clicked", values=[VALUE]))
    return job, ticket


def recorded(response):
    return (response.status_code, response.json()) == (200, {"ok": True, "recorded": True})


def nothing_recorded(conn, job):
    return one(conn, "SELECT count(*) FROM submissions")[0] == 0 and one(
        conn, "SELECT stage FROM tracker WHERE job_id = ?", job
    ) == ("ready",)


# Shipped off


@pytest.mark.parametrize("site", [greenhouse, lever, ashby])
def test_every_site_ships_off(site):
    assert site.DRAWN.thanks is None


def test_the_plugin_is_told_no_sign(door, conn, data_dir, paired):
    press(door, conn, data_dir, ready_job(conn))
    assert call(door, START).json()["thanks"] is None


def test_the_plugin_is_told_a_site_s_sign_word_for_word(door, conn, data_dir, paired, monkeypatch):
    sign = Thanks(selector="h1.done", words="Thank you")
    monkeypatch.setattr(greenhouse, "DRAWN", replace(greenhouse.DRAWN, thanks=sign))
    press(door, conn, data_dir, ready_job(conn))
    assert call(door, START).json()["thanks"] == {"path": "", "selector": "h1.done", "words": "Thank you"}


def test_a_site_that_is_off_refuses_even_after_a_real_press(door, conn, data_dir, paired):
    job, ticket = pressed(door, conn, data_dir)
    assert refused(report(door, ticket, "thanked"), 400, "bad-request")
    assert nothing_recorded(conn, job) and held(door) and fill_rows(conn)[0][2:] == ("sent", None)


# Each refusal, with the site switched on


def test_no_press_refuses(door, conn, data_dir, paired, site_on):
    job = on_board(conn, data_dir)
    ticket = answered(door, conn, data_dir, job)
    assert ok(report(door, ticket, "reread", values=[VALUE]))
    assert refused(report(door, ticket, "thanked"), 400, "bad-request")
    assert nothing_recorded(conn, job) and held(door)


def test_a_press_over_two_minutes_old_refuses(door, conn, data_dir, paired, site_on, clock):
    job, ticket = pressed(door, conn, data_dir)
    clock.now += fills.THANKS_SECONDS + 1
    assert refused(report(door, ticket, "thanked"), 400, "bad-request")
    assert nothing_recorded(conn, job) and held(door)


def test_a_press_exactly_two_minutes_old_counts(door, conn, data_dir, paired, site_on, clock):
    assert fills.THANKS_SECONDS == 2 * 60
    job, ticket = pressed(door, conn, data_dir)
    clock.now += fills.THANKS_SECONDS
    assert recorded(report(door, ticket, "thanked"))


def test_a_newer_press_starts_the_two_minutes_again(door, conn, data_dir, paired, site_on, clock):
    _, ticket = pressed(door, conn, data_dir)
    clock.now += 100
    assert ok(report(door, ticket, "clicked", values=[VALUE]))
    clock.now += 100
    assert recorded(report(door, ticket, "thanked"))


def test_answers_not_out_refuses(door, conn, data_dir, paired, site_on, clock):
    job = on_board(conn, data_dir)
    ticket = running(door, conn, data_dir, job)
    held(door)[job].clicked = clock.now  # no report can do this before the answers, so this rule stands alone here
    assert refused(report(door, ticket, "thanked"), 400, "bad-request")
    assert nothing_recorded(conn, job) and held(door)


@pytest.mark.parametrize("kind", ["limit", "other", "", 0, False, ["limit"]])
def test_a_kind_refuses(door, conn, data_dir, paired, site_on, kind):
    job, ticket = pressed(door, conn, data_dir)
    assert refused(report(door, ticket, "thanked", kind=kind), 400, "bad-request")
    assert nothing_recorded(conn, job) and held(door)


@pytest.mark.parametrize(
    "body",
    [
        *({"values": [VALUE | {"value": value}]} for value in BAD_VALUES),
        *({"values": [VALUE | key]} for key in BAD_KEYS),
        {"values": None},
        {"left": [LEFT | {"why": "made-up"}]},
        {"left": None},
    ],
)
def test_a_bad_body_refuses_and_records_nothing(door, conn, data_dir, paired, site_on, body):
    job, ticket = pressed(door, conn, data_dir)
    assert refused(report(door, ticket, "thanked", **body), 400, "bad-request")
    assert nothing_recorded(conn, job) and held(door)


def test_another_browser_s_ticket_is_no_ticket(door, conn, data_dir, paired, site_on):
    pair(conn, SECOND_KEY, "Desk", OTHER_PLUGIN)
    job, ticket = pressed(door, conn, data_dir)
    assert refused(report(door, ticket, "thanked", SECOND_KEY, OTHER_PLUGIN), 404, "no-ticket")
    assert nothing_recorded(conn, job) and held(door)


def test_a_login_that_ran_out_records_nothing_and_it_still_looks_sent(owner, door, conn, data_dir, paired, site_on):
    job, ticket = pressed(door, conn, data_dir)
    log_out(owner)
    assert refused(report(door, ticket, "thanked"), 403, "not-logged-in")
    assert nothing_recorded(conn, job) and held(door)
    log_in(owner)
    assert LOOKS_SENT in owner.get(f"/jobs/{job}/form").text


def test_a_busy_database_records_nothing_and_keeps_the_ticket(door, conn, data_dir, paired, site_on):
    job, ticket = pressed(door, conn, data_dir)
    conn.execute("BEGIN IMMEDIATE")
    response = report(door, ticket, "thanked")
    conn.rollback()
    assert refused(response, 503, "app-error")
    assert nothing_recorded(conn, job) and held(door) and fill_rows(conn)[0][2:] == ("sent", None)


# All good


def test_it_records_moves_the_card_and_ends_the_fill(owner, door, conn, data_dir, paired, site_on):
    job, ticket = pressed(door, conn, data_dir)
    assert recorded(report(door, ticket, "thanked"))
    found = record.recorded(conn, job)
    assert found["how"] == "browser" and found["checked_on"] is not None
    assert one(conn, "SELECT stage FROM tracker WHERE job_id = ?", job) == ("applied",)
    assert not held(door) and fill_rows(conn)[0][2:] == ("sent", "thanked")
    assert one(conn, "SELECT values_read, checked_on FROM fills") == (None, None)
    assert fill_words.latest(conn, job)["what"] == SAID
    page = owner.get(f"/jobs/{job}/form").text
    assert LOOKS_SENT not in page and ">I submitted it</button>" not in page


def test_a_filled_fill_says_so_too(door, conn, data_dir, paired, site_on):
    job = on_board(conn, data_dir)
    ticket = answered(door, conn, data_dir, job)
    assert ok(report(door, ticket, values=[VALUE]))
    assert ok(report(door, ticket, "clicked", values=[VALUE]))
    assert recorded(report(door, ticket, "thanked"))
    assert fill_rows(conn)[0][2:] == ("filled", "thanked") and fill_words.latest(conn, job)["what"] == SAID


def test_a_second_sign_is_no_ticket_and_records_once(door, conn, data_dir, paired, site_on):
    job, ticket = pressed(door, conn, data_dir)
    assert recorded(report(door, ticket, "thanked"))
    before = record.recorded(conn, job)
    assert refused(report(door, ticket, "thanked"), 404, "no-ticket")
    assert record.recorded(conn, job) == before and one(conn, "SELECT count(*) FROM submissions")[0] == 1


def test_with_no_read_held_the_record_is_by_hand(door, conn, data_dir, paired, site_on):
    job = on_board(conn, data_dir)
    ticket = answered(door, conn, data_dir, job)
    assert ok(report(door, ticket, "clicked", values=[]))
    assert recorded(report(door, ticket, "thanked"))
    assert record.recorded(conn, job)["how"] == "by hand"


def test_a_card_past_ready_stays_where_it_is(door, conn, data_dir, paired, site_on):
    job, ticket = pressed(door, conn, data_dir, APPLIED)
    assert recorded(report(door, ticket, "thanked"))
    assert record.recorded(conn, job)["how"] == "browser"
    assert one(conn, "SELECT applied_on FROM tracker WHERE job_id = ?", job) == ("2026-10-17",)


# What can't be recorded is no error


def test_no_card_changes_nothing_and_no_error_reaches_the_plugin(owner, door, conn, data_dir, paired, site_on):
    job = ready_job(conn)
    ticket = answered(door, conn, data_dir, job)
    assert ok(report(door, ticket, "clicked", values=[VALUE]))
    answer = report(door, ticket, "thanked")
    assert (answer.status_code, answer.json()) == (200, {"ok": True, "recorded": False})
    assert record.recorded(conn, job) is None and held(door) and fill_rows(conn)[0][2:] == ("sent", None)
    assert one(conn, "SELECT values_read FROM fills")[0] is not None
    assert LOOKS_SENT in owner.get(f"/jobs/{job}/form").text


def test_already_recorded_closes_the_fill_and_records_nothing_new(
    owner, door, conn, data_dir, paired, site_on, monkeypatch
):
    job, ticket = pressed(door, conn, data_dir)
    data = {"on": date.today().isoformat(), "csrf_token": csrf(owner)}
    with monkeypatch.context() as moment:  # the ticket outlives the record, as a busy database leaves it
        moment.setattr(owner.app.state.tickets, "end_job", lambda *_: None)
        assert owner.post(f"/jobs/{job}/form/submitted", data=data).status_code == 200
    before = record.recorded(conn, job)
    assert held(door) and before is not None
    assert refused(report(door, ticket, "thanked"), 404, "no-ticket")
    assert record.recorded(conn, job) == before and not held(door)
