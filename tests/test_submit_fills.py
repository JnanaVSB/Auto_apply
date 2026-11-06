"""Fills: the form's address, the rules for one, the row that counts it and its ticket, with a fake clock."""

import inspect
import json
import re
from contextlib import closing, contextmanager
from pathlib import Path

import pytest
import sqlcipher3
from helpers import add_job, one
from pydantic import ValidationError
from test_submit_lever import PAGE as LEVER_PAGE

from app import db
from app.config import Config
from app.sources import closed_site, is_simplify_site, posting_of
from app.submit import Choice, Field, Form, ashby, fills, greenhouse, lever, match
from app.submit.preview import Preview
from app.web import queries, security

NOON = "2026-11-06T12:00:00+00:00"
EARLIER = "2026-11-06T09:00:00+00:00"
YESTERDAY = "2026-11-05T12:00:00+00:00"
LATER = "2026-11-07T12:00:00+00:00"
LINK = "https://job-boards.greenhouse.io/acme/jobs/123"
ADDRESS = "https://job-boards.greenhouse.io/embed/job_app?for=acme&token=123"
READY = Preview(EARLIER, "api", None, (), (), 0, False)
LIMITS = Config(data_dir=Path("data"), fills_per_day=3, fill_pause_minutes=0)
PAUSED = LIMITS.model_copy(update={"fill_pause_minutes": 2})
ROW = "SELECT address, started_on, toolbar, browser, state, stop_kind FROM fills WHERE id = ?"


@pytest.fixture
def at(monkeypatch):
    """Sets what time the app thinks it is; noon until a test says otherwise."""

    def at(moment):
        monkeypatch.setattr(fills, "now", lambda: moment)

    at(NOON)
    return at


@pytest.fixture
def laptop(conn):
    """A browser that has connected; its number."""
    return browser(conn, "Laptop", "chrome-extension://" + "a" * 32)


@pytest.fixture
def tickets(clock):
    return fills.Tickets(clock)


def browser(conn, name, plugin):
    add = "INSERT INTO paired_browsers (name, key_hash, plugin, added_on) VALUES (?, ?, ?, 'then')"
    with conn:
        return conn.execute(add, (name, f"hash-{name}", plugin)).lastrowid


def job(conn, link=LINK):
    with conn:
        return queries.job(conn, add_job(conn, "ML Intern", apply_url=link))


def past_fill(conn, job_id, started_on, state="ended"):
    add = "INSERT INTO fills (job_id, address, started_on, toolbar, state) VALUES (?, ?, ?, 0, ?)"
    with conn:
        return conn.execute(add, (job_id, ADDRESS, started_on, state)).lastrowid


def rows(conn):
    return one(conn, "SELECT count(*) FROM fills")[0]


@contextmanager
def busy(conn, data_dir):
    """Another connection holds the database, and `conn` doesn't wait for it."""
    conn.execute("PRAGMA busy_timeout = 0")
    with closing(db.connect(data_dir)) as other:
        other.execute("BEGIN IMMEDIATE")
        yield
        other.rollback()


def only(tickets):
    (ticket,) = tickets.tickets.values()
    return ticket


# The form's address


@pytest.mark.parametrize(
    ("link", "address", "module"),
    [
        (LINK, ADDRESS, greenhouse),
        ("https://jobs.lever.co/acme/0a1b-2c3d", "https://jobs.lever.co/acme/0a1b-2c3d/apply", lever),
        ("https://jobs.ashbyhq.com/acme/0a1b-2c3d", "https://jobs.ashbyhq.com/acme/0a1b-2c3d/application", ashby),
    ],
)
def test_each_site_s_form_address_is_open_to_the_app_and_leads_back_to_the_job(link, address, module):
    built = fills.form_address({"apply_url": link, "open_manually_only": False})
    assert built == address == module.FORM_PAGE.format(*posting_of(link)[1:])
    assert not closed_site(built) and not is_simplify_site(built)
    assert posting_of(built) == posting_of(link)


def test_a_form_address_is_always_https():
    built = fills.form_address({"apply_url": "http://jobs.lever.co/acme/1", "open_manually_only": False})
    assert built == "https://jobs.lever.co/acme/1/apply"


@pytest.mark.parametrize(
    "job_",
    [
        {"apply_url": "https://acme.example/careers/1", "open_manually_only": False},
        {"apply_url": "https://simplify.jobs/p/1", "open_manually_only": False},
        {"apply_url": LINK, "open_manually_only": True},
    ],
)
def test_no_form_address_without_a_posting_or_for_a_job_opened_by_hand(job_):
    assert fills.form_address(job_) is None


@pytest.mark.parametrize("page", ["https://www.linkedin.com/{}/{}", "https://simplify.jobs/{}/{}"])
def test_an_address_on_a_site_the_app_never_opens_is_no_address(monkeypatch, page):
    monkeypatch.setattr(greenhouse, "FORM_PAGE", page)
    assert fills.form_address({"apply_url": LINK, "open_manually_only": False}) is None


def test_each_board_says_what_its_drawn_form_shows_differently():
    drawn = greenhouse.DRAWN
    assert drawn.undrawn == {"race", "school", "degree", "discipline"}
    assert drawn.undrawn_starts == ("demographic_", "data_compliance[")
    assert drawn.unlisted == {"country", "hispanic_ethnicity"}
    assert drawn.hidden_choices
    for quiet in (lever.DRAWN, ashby.DRAWN):
        assert (quiet.undrawn, quiet.undrawn_starts, quiet.unlisted, quiet.hidden_choices) == (set(), (), set(), False)
    assert {site.DRAWN.file_checks for site in (greenhouse, lever, ashby)} == {("kind",)}


# The settings


def test_the_two_numbers_have_their_defaults():
    config = Config(data_dir=Path("data"))
    assert (config.fills_per_day, config.fill_pause_minutes) == (10, 2)


@pytest.mark.parametrize(
    ("setting", "value", "ok"),
    [
        ("fills_per_day", 1, True), ("fills_per_day", 50, True), ("fills_per_day", 0, False),
        ("fills_per_day", 51, False), ("fills_per_day", "5", False), ("fills_per_day", 1.5, False),
        ("fill_pause_minutes", 0, True), ("fill_pause_minutes", 60, True), ("fill_pause_minutes", -1, False),
        ("fill_pause_minutes", 61, False), ("fill_pause_minutes", "2", False),
    ],
)  # fmt: skip
def test_the_two_numbers_have_limits(setting, value, ok):
    if ok:
        assert getattr(Config(data_dir=Path("data"), **{setting: value}), setting) == value
    else:
        with pytest.raises(ValidationError):
            Config(data_dir=Path("data"), **{setting: value})


# The rules


def test_a_ready_job_may_be_filled(conn, laptop, at):
    assert fills.why_not(conn, job(conn), READY, LIMITS) is None


def test_no_address_and_no_saved_read_refuse_with_no_words(conn, laptop, at):
    assert fills.why_not(conn, job(conn, "https://acme.example/careers/1"), READY, LIMITS) == ""
    assert fills.why_not(conn, job(conn), None, LIMITS) == ""


def test_a_job_with_a_record_refuses_with_no_words(conn, laptop, at):
    found = job(conn)
    with conn:
        conn.execute(
            "INSERT INTO submissions (job_id, submitted_on, saved_on, how, address, fields, files) "
            "VALUES (?, '2026-11-05', 'then', 'by hand', ?, '[]', '[]')",
            (found["id"], ADDRESS),
        )
    assert fills.why_not(conn, found, READY, LIMITS) == ""


def test_open_decisions_come_before_the_browser_rule(conn, at):
    still_open = Preview(EARLIER, "api", None, (), (), 1, False)
    words = "Answer the decisions above first, then Open and fill appears."
    assert fills.why_not(conn, job(conn), still_open, LIMITS) == words


@pytest.mark.parametrize("key_made", [False, True])
def test_a_browser_must_have_connected(conn, at, key_made):
    if key_made:
        browser(conn, "Never used", None)  # a key never pasted
    assert fills.why_not(conn, job(conn), READY, LIMITS) == "Connect a browser first: Settings → Browser plugin."


def test_the_cap_refuses_and_counts_every_row_whatever_became_of_it(conn, laptop, at):
    found = job(conn)
    for state in ("stopped", "ended", "sent"):
        past_fill(conn, add_job(conn, "Other"), EARLIER, state)
    words = "Today's 3 fills are used: more tomorrow, or raise the number in Settings."
    assert fills.why_not(conn, found, READY, LIMITS) == words


def test_a_cap_of_one_reads_as_one(conn, laptop, at):
    found = job(conn)
    past_fill(conn, add_job(conn, "Other"), EARLIER)
    limit = LIMITS.model_copy(update={"fills_per_day": 1})
    words = "Today's 1 fill is used: more tomorrow, or raise the number in Settings."
    assert fills.why_not(conn, found, READY, limit) == words


def test_a_lowered_cap_refuses_though_more_were_used(conn, laptop, at):
    found = job(conn)
    for _ in range(5):
        past_fill(conn, add_job(conn, "Other"), EARLIER)
    assert "Today's 3 fills are used" in fills.why_not(conn, found, READY, LIMITS)


def test_yesterday_s_fills_are_not_in_today_s_count(conn, laptop, at):
    found = job(conn)
    for _ in range(5):
        past_fill(conn, add_job(conn, "Other"), YESTERDAY)
    assert fills.why_not(conn, found, READY, LIMITS) is None


@pytest.mark.parametrize(
    ("newest", "words"),
    [
        ("2026-11-06T11:59:30+00:00", "2 minutes"),  # 90 seconds left, rounded up
        ("2026-11-06T11:58:30+00:00", "1 minute"),
    ],
)
def test_the_pause_says_the_minutes_left_for_any_job(conn, laptop, at, newest, words):
    found = job(conn)
    past_fill(conn, add_job(conn, "Other"), newest)
    said = f"The next fill can start in {words}: that's the pause you set in Settings."
    assert fills.why_not(conn, found, READY, PAUSED) == said


def test_the_pause_ends_on_time_and_0_is_off(conn, laptop, at):
    found = job(conn)
    past_fill(conn, add_job(conn, "Other"), "2026-11-06T11:58:00+00:00")
    assert fills.why_not(conn, found, READY, PAUSED) is None
    past_fill(conn, add_job(conn, "Other"), NOON)
    assert fills.why_not(conn, found, READY, LIMITS) is None


def test_a_fill_dated_in_the_future_counts_as_past(conn, laptop, at):
    found = job(conn)
    for _ in range(5):
        past_fill(conn, add_job(conn, "Other"), LATER)
    assert fills.why_not(conn, found, READY, PAUSED) is None  # not in today's count, and no wait


def test_an_evening_in_phoenix_is_still_today(conn, laptop, at, phoenix):
    found = job(conn)
    limit = LIMITS.model_copy(update={"fills_per_day": 2})
    past_fill(conn, add_job(conn, "Other"), "2026-11-05T06:59:59+00:00")  # 23:59:59 the night before: not today
    past_fill(conn, add_job(conn, "Other"), "2026-11-05T07:00:00+00:00")  # local midnight
    at("2026-11-06T05:30:00+00:00")  # 22:30, already tomorrow in UTC
    assert fills.why_not(conn, found, READY, limit) is None
    past_fill(conn, add_job(conn, "Other"), "2026-11-05T20:00:00+00:00")  # 13:00, yesterday in UTC, today here
    assert "Today's 2 fills are used" in fills.why_not(conn, found, READY, limit)
    at("2026-11-06T06:59:59+00:00")  # 23:59:59
    assert "Today's 2 fills are used" in fills.why_not(conn, found, READY, limit)
    at("2026-11-06T07:00:00+00:00")  # midnight here: a new day
    assert fills.why_not(conn, found, READY, limit) is None


# Making a fill


def test_a_press_saves_a_row_then_makes_a_ticket(conn, laptop, at, tickets, clock):
    found = job(conn)
    assert tickets.make(conn, found, READY, LIMITS) is None
    ticket = only(tickets)
    assert one(conn, ROW, ticket.fill) == (ADDRESS, NOON, 0, None, "waiting", None)
    assert (ticket.job, ticket.address, ticket.site, ticket.browser, ticket.taken) == (
        found["id"], ADDRESS, "greenhouse", None, False,
    )  # fmt: skip
    assert len(ticket.id) >= 43 and ticket.ends == clock.now + fills.UNUSED_SECONDS  # 256 bits


def test_a_toolbar_press_names_its_browser_at_once(conn, laptop, at, tickets):
    tickets.make(conn, job(conn), READY, LIMITS, laptop, "Laptop")
    assert one(conn, ROW, only(tickets).fill)[2:5] == (1, "Laptop", "waiting")
    assert only(tickets).browser == laptop


def test_two_presses_straight_after_each_other_can_t_both_slip_under_the_cap(conn, laptop, at, tickets):
    limit = LIMITS.model_copy(update={"fills_per_day": 1})
    first, second = (job(conn, f"https://jobs.lever.co/acme/{number}") for number in range(2))
    assert not inspect.iscoroutinefunction(tickets.make)  # nothing can run between its rules and its row
    assert tickets.make(conn, first, READY, limit) is None
    assert "Today's 1 fill is used" in tickets.make(conn, second, READY, limit)
    assert rows(conn) == 1 and only(tickets).job == first["id"]


def test_a_refused_press_gives_the_reason_and_leaves_no_row_or_ticket(conn, at, tickets):
    found = job(conn)
    assert tickets.make(conn, found, READY, LIMITS) == "Connect a browser first: Settings → Browser plugin."
    assert tickets.make(conn, found, None, LIMITS) == ""
    assert rows(conn) == 0 and not tickets.tickets


def test_a_repeat_press_on_an_unused_fill_neither_counts_nor_waits(conn, laptop, at, tickets):
    found = job(conn)
    tickets.make(conn, found, READY, PAUSED)
    first = only(tickets)
    for _ in range(4):
        past_fill(conn, add_job(conn, "Other"), NOON)  # the cap is reached and the pause hasn't passed
    assert fills.why_not(conn, found, READY, PAUSED) is None
    assert tickets.make(conn, found, READY, PAUSED) is None
    assert rows(conn) == 5
    assert only(tickets).fill == first.fill and only(tickets).id != first.id  # the old ticket is replaced
    assert one(conn, ROW, first.fill)[1] == NOON


def test_a_repeat_press_keeps_the_time_and_takes_in_a_toolbar_press(conn, laptop, at, tickets):
    found = job(conn)
    tickets.make(conn, found, READY, LIMITS)
    at("2026-11-06T12:01:00+00:00")
    tickets.make(conn, found, READY, LIMITS, laptop, "Laptop")
    assert one(conn, ROW, only(tickets).fill)[1:4] == (NOON, 1, "Laptop")
    assert rows(conn) == 1


def test_a_button_press_keeps_the_trail_of_a_toolbar_press(conn, laptop, at, tickets):
    found = job(conn)
    tickets.make(conn, found, READY, LIMITS, laptop, "Laptop")
    tickets.make(conn, found, READY, LIMITS)
    assert one(conn, ROW, only(tickets).fill)[1:4] == (NOON, 1, "Laptop")
    assert only(tickets).browser is None and rows(conn) == 1  # the new ticket is the button's: any browser can take it


def test_an_unused_fill_stays_usable_after_its_ticket_ran_out(conn, laptop, at, tickets, clock):
    found = job(conn)
    tickets.make(conn, found, READY, PAUSED)
    fill = only(tickets).fill
    clock.now += fills.UNUSED_SECONDS + 1
    assert tickets.make(conn, found, READY, PAUSED) is None
    assert only(tickets).fill == fill and rows(conn) == 1
    assert only(tickets).ends == clock.now + fills.UNUSED_SECONDS


def test_an_unused_fill_stays_usable_after_a_restart(conn, laptop, at, clock):
    found = job(conn)
    fill = past_fill(conn, found["id"], EARLIER, "waiting")
    restarted = fills.Tickets(clock)
    assert restarted.make(conn, found, READY, LIMITS) is None
    assert only(restarted).fill == fill and rows(conn) == 1


@pytest.mark.parametrize("started_on", [YESTERDAY, LATER])
def test_a_waiting_row_not_from_today_makes_a_new_row(conn, laptop, at, tickets, started_on):
    found = job(conn)
    old = past_fill(conn, found["id"], started_on, "waiting")
    assert tickets.make(conn, found, READY, LIMITS) is None
    assert only(tickets).fill != old and rows(conn) == 2


def test_another_job_s_unused_fill_is_not_reused(conn, laptop, at, tickets):
    past_fill(conn, add_job(conn, "Other"), EARLIER, "waiting")
    found = job(conn)
    tickets.make(conn, found, READY, LIMITS)
    assert rows(conn) == 2


def test_a_busy_database_leaves_no_ticket(conn, data_dir, laptop, at, tickets):
    found = job(conn)
    with busy(conn, data_dir), pytest.raises(sqlcipher3.OperationalError):
        tickets.make(conn, found, READY, LIMITS)
    assert not tickets.tickets and rows(conn) == 0


def test_a_press_on_a_running_fill_ends_it_and_counts(conn, laptop, at, tickets):
    found = job(conn)
    tickets.make(conn, found, READY, PAUSED)
    old = tickets.take(conn, ADDRESS, laptop, "Laptop")
    at("2026-11-06T12:02:00+00:00")
    assert tickets.make(conn, found, READY, PAUSED) is None
    assert one(conn, ROW, old.fill)[3:] == ("Laptop", "ended", "replaced")
    assert only(tickets).fill != old.fill and rows(conn) == 2
    assert one(conn, ROW, only(tickets).fill)[1:5] == ("2026-11-06T12:02:00+00:00", 0, None, "waiting")
    assert tickets.running(old.id, laptop) is None


@pytest.mark.parametrize("limits", [PAUSED, LIMITS.model_copy(update={"fills_per_day": 1})])
def test_a_refused_press_leaves_the_running_fill_alone(conn, laptop, at, tickets, limits):
    found = job(conn)
    tickets.make(conn, found, READY, LIMITS)
    old = tickets.take(conn, ADDRESS, laptop, "Laptop")
    assert tickets.make(conn, found, READY, limits)  # the pause, then the cap: it counts as a new fill
    assert tickets.running(old.id, laptop) is old
    assert one(conn, ROW, old.fill)[4:] == ("started", None) and rows(conn) == 1


def test_a_busy_database_leaves_the_running_fill_alone(conn, data_dir, laptop, at, tickets):
    found = job(conn)
    tickets.make(conn, found, READY, LIMITS)
    old = tickets.take(conn, ADDRESS, laptop, "Laptop")
    with busy(conn, data_dir), pytest.raises(sqlcipher3.OperationalError):
        tickets.make(conn, found, READY, LIMITS)
    assert tickets.running(old.id, laptop) is old
    assert one(conn, ROW, old.fill)[4:] == ("started", None) and rows(conn) == 1


def test_a_running_fill_that_ran_out_is_simply_gone(conn, laptop, at, tickets, clock):
    found = job(conn)
    tickets.make(conn, found, READY, LIMITS)
    old = tickets.take(conn, ADDRESS, laptop, "Laptop")
    clock.now += fills.RUNNING_SECONDS + 1
    assert tickets.make(conn, found, READY, LIMITS) is None
    assert one(conn, ROW, old.fill)[4:] == ("started", None) and rows(conn) == 2


# Taking a ticket


def test_a_button_ticket_goes_to_whichever_browser_opens_the_form_once(conn, laptop, at, tickets, clock):
    tickets.make(conn, job(conn), READY, LIMITS)
    assert tickets.take(conn, ADDRESS + "&x=1", laptop, "Laptop") is None
    assert one(conn, ROW, only(tickets).fill)[3:5] == (None, "waiting")
    ticket = tickets.take(conn, ADDRESS, laptop, "Laptop")
    assert (ticket.browser, ticket.taken, ticket.ends) == (laptop, True, clock.now + fills.RUNNING_SECONDS)
    assert one(conn, ROW, ticket.fill)[3:5] == ("Laptop", "started")
    assert tickets.take(conn, ADDRESS, laptop, "Laptop") is None  # a reload finds nothing


def test_a_toolbar_ticket_can_be_taken_only_by_its_browser(conn, laptop, at, tickets):
    other = browser(conn, "Desk", "chrome-extension://" + "b" * 32)
    tickets.make(conn, job(conn), READY, LIMITS, laptop, "Laptop")
    assert tickets.take(conn, ADDRESS, other, "Desk") is None
    assert one(conn, ROW, only(tickets).fill)[3:5] == ("Laptop", "waiting")
    assert tickets.take(conn, ADDRESS, laptop, "Laptop") is only(tickets)


def test_a_ticket_lasts_5_minutes_unused(conn, laptop, at, tickets, clock):
    tickets.make(conn, job(conn), READY, LIMITS)
    clock.now += fills.UNUSED_SECONDS + 1
    assert fills.UNUSED_SECONDS == 5 * 60
    assert tickets.take(conn, ADDRESS, laptop, "Laptop") is None


def test_a_ticket_can_be_taken_at_the_last_moment(conn, laptop, at, tickets, clock):
    tickets.make(conn, job(conn), READY, LIMITS)
    clock.now += fills.UNUSED_SECONDS
    assert tickets.take(conn, ADDRESS, laptop, "Laptop") is not None


def test_a_taken_ticket_lasts_2_hours(conn, laptop, at, tickets, clock):
    tickets.make(conn, job(conn), READY, LIMITS)
    ticket = tickets.take(conn, ADDRESS, laptop, "Laptop")
    clock.now += fills.RUNNING_SECONDS
    assert fills.RUNNING_SECONDS == 2 * 60 * 60
    assert tickets.running(ticket.id, laptop) is ticket
    clock.now += 1
    assert tickets.running(ticket.id, laptop) is None


def test_a_busy_database_uses_nothing_up(conn, data_dir, laptop, at, tickets):
    tickets.make(conn, job(conn), READY, LIMITS)
    with busy(conn, data_dir), pytest.raises(sqlcipher3.OperationalError):
        tickets.take(conn, ADDRESS, laptop, "Laptop")
    assert not only(tickets).taken and one(conn, ROW, only(tickets).fill)[4] == "waiting"
    assert tickets.take(conn, ADDRESS, laptop, "Laptop") is not None


def test_only_a_taken_ticket_of_this_browser_is_running(conn, laptop, at, tickets):
    other = browser(conn, "Desk", "chrome-extension://" + "b" * 32)
    tickets.make(conn, job(conn), READY, LIMITS)
    assert tickets.running(only(tickets).id, laptop) is None  # not taken yet
    ticket = tickets.take(conn, ADDRESS, laptop, "Laptop")
    assert tickets.running(ticket.id, laptop) is ticket
    assert tickets.running(ticket.id, other) is None
    assert tickets.running("no such ticket", laptop) is None


# Ending


def three_fills(conn, tickets, laptop):
    """This browser's running fill, its toolbar fill not yet started, and a button fill nobody took."""
    links = [f"https://jobs.lever.co/acme/{number}" for number in range(3)]
    running, toolbar, button = (job(conn, link) for link in links)
    tickets.make(conn, running, READY, LIMITS)
    tickets.take(conn, f"{links[0]}/apply", laptop, "Laptop")
    tickets.make(conn, toolbar, READY, LIMITS, laptop, "Laptop")
    tickets.make(conn, button, READY, LIMITS)
    return [tickets.tickets[found["id"]] for found in (running, toolbar, button)]


def test_removing_a_browser_ends_its_tickets_and_no_button_ticket_nobody_took(conn, laptop, at, tickets):
    running, toolbar, button = three_fills(conn, tickets, laptop)
    with conn:
        tickets.end_browser(conn, laptop)
    assert only(tickets) is button
    for ticket in (running, toolbar):
        assert one(conn, ROW, ticket.fill)[3:] == ("Laptop", "ended", "browser-removed")
    assert one(conn, ROW, button.fill)[4:] == ("waiting", None)


def test_another_browser_s_tickets_stay(conn, laptop, at, tickets):
    other = browser(conn, "Desk", "chrome-extension://" + "b" * 32)
    tickets.make(conn, job(conn), READY, LIMITS, laptop, "Laptop")
    with conn:
        tickets.end_browser(conn, other)
    assert only(tickets).browser == laptop


def test_logging_out_all_devices_ends_every_ticket(conn, laptop, at, tickets):
    made = three_fills(conn, tickets, laptop)
    with conn:
        tickets.end_all(conn)
    assert not tickets.tickets
    for ticket in made:
        assert one(conn, ROW, ticket.fill)[4:] == ("ended", "browser-removed")


def test_i_submitted_it_ends_that_job_s_ticket(conn, laptop, at, tickets):
    running, toolbar, button = three_fills(conn, tickets, laptop)
    with conn:
        tickets.end_job(conn, running.job)
    assert one(conn, ROW, running.fill)[4:] == ("ended", "submitted")
    assert len(tickets.tickets) == 2 and tickets.running(running.id, laptop) is None
    with conn:
        tickets.end_job(conn, running.job)  # nothing left to end
    assert len(tickets.tickets) == 2


@pytest.mark.parametrize("end", ["end_browser", "end_all", "end_job"])
def test_the_caller_s_save_holds_the_marks(conn, laptop, at, tickets, end):
    running, *_ = three_fills(conn, tickets, laptop)
    getattr(tickets, end)(conn, *{"end_browser": (laptop,), "end_all": (), "end_job": (running.job,)}[end])
    conn.rollback()  # the caller's save fails after the marks
    assert one(conn, ROW, running.fill)[4:] == ("started", None)


def test_a_failed_mark_ends_nothing(conn, data_dir, laptop, at, tickets):
    running, *_ = three_fills(conn, tickets, laptop)
    with busy(conn, data_dir), pytest.raises(sqlcipher3.OperationalError), conn:
        tickets.end_all(conn)
    assert len(tickets.tickets) == 3


def test_a_ticket_past_its_time_is_not_marked_when_things_end(conn, laptop, at, tickets, clock):
    running, *_ = three_fills(conn, tickets, laptop)
    clock.now += fills.RUNNING_SECONDS + 1
    with conn:
        tickets.end_all(conn)
    assert one(conn, ROW, running.fill)[4:] == ("started", None)


# The read-only login check


def session(started, last_seen):
    return security.Session("fake-token", started, last_seen)


def test_nobody_logged_in_is_not_live(clock):
    assert not security.Logins(clock).live()


def test_live_finds_a_login_and_marks_nothing(clock):
    logins = security.Logins(clock, {"one": session(clock.now, clock.now)})
    clock.now += security.IDLE_SECONDS
    assert logins.live()
    assert logins.sessions["one"].last_seen == clock.now - security.IDLE_SECONDS  # the plugin keeps no login alive


def test_live_applies_the_idle_limit_and_deletes_nothing(clock):
    logins = security.Logins(clock, {"one": session(clock.now, clock.now)})
    clock.now += security.IDLE_SECONDS + 1
    assert not logins.live()
    assert "one" in logins.sessions
    assert logins.session("one") is None and "one" not in logins.sessions  # the usual check still forgets it


def test_live_applies_the_twelve_hour_limit_though_in_use(clock):
    logins = security.Logins(clock, {"one": session(clock.now - security.MAX_SESSION_SECONDS - 1, clock.now)})
    assert not logins.live()
    assert "one" in logins.sessions


def test_one_live_login_among_ended_ones_is_enough(clock):
    logins = security.Logins(clock, {"old": session(0.0, 0.0), "new": session(clock.now, clock.now)})
    clock.now += security.IDLE_SECONDS
    assert logins.live()


# What the browser says the form shows


GONE, NEW = "no longer on the form", "new on the form"
WORDS, KIND, REQUIRED = "the question's words changed", "the kind of box changed", "whether it's required changed"
CHOICES, CHOICES_UNCHECKED = "its choices changed", "its choices weren't checked"
FILE_UNCHECKED, UNCHECKED = "its words and whether it's required weren't checked", "wasn't checked"
RESUME_PDF = "/fake/folder/resume-v3.pdf"
DOCUMENTS = {"resume": ("Resume v3", Path(RESUME_PDF)), "cover_letter": None}
GOOD_BOX = {"key": "email", "label": "Email", "kind": "text", "required": True}


def box(key, label, kind="text", required=False, **rest):
    return {"key": key, "label": label, "kind": kind, "required": required} | rest


def offered(*labels):
    return [{"label": label, "value": f"site-{number}"} for number, label in enumerate(labels)]


def lines_of(form, values):
    """A line for each field: filled with `values[key]`, else empty, and a hidden one hidden."""
    return tuple(
        match.Line(field, "hidden") if field.kind == "hidden"
        else match.Line(field, "fill", values[field.key]) if field.key in values
        else match.Line(field, "empty")
        for field in form.fields
    )  # fmt: skip


def compared(lines, boxes, board=lever.DRAWN):
    return fills.compare(lines, fills.drawn(boxes), board)


def keys(passed):
    return [line.field.key for line, _ in passed]


def said(changes):
    return {change["question"]: change["what"] for change in changes}


# Greenhouse, as the plan's "Real forms read early" found it: the app's read, then what the browser draws
GREENHOUSE_REPLY = {
    "questions": [
        {"label": "First Name", "required": True, "fields": [{"name": "first_name", "type": "input_text"}]},
        {"label": "Email", "required": True, "fields": [{"name": "email", "type": "input_text"}]},
        {"label": "Resume/CV", "required": True, "fields": [
            {"name": "resume", "type": "input_file"}, {"name": "resume_text", "type": "textarea"},
        ]},
        {"label": "Need sponsorship?", "required": True, "fields": [
            {"name": "question_1", "type": "multi_value_single_select",
             "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}]},
        ]},
    ],
    "compliance": [{"questions": [{"label": "Race", "required": False, "fields": [
        {"name": "race", "type": "multi_value_single_select", "values": [{"label": "Decline", "value": 9}]},
    ]}]}],
    "demographic_questions": {"questions": [
        {"id": 7, "label": "Pronouns", "type": "multi_value_single_select",
         "answer_options": [{"id": 1, "label": "He"}]},
    ]},
    "data_compliance": [{"type": "gdpr", "requires_consent": True}],
    "education": "education_optional",
}  # fmt: skip
GREENHOUSE_DRAWN = [
    box("first_name", "First Name*", required=True),
    box("email", "Email*", required=True),
    box("resume", "Attach", "file"),
    box("country", "Country*", "single", True),
    box("question_1", "Need sponsorship?*", "single", True),
    box("hispanic_ethnicity", "Are you Hispanic or Latino?", "single"),
]
GREENHOUSE_VALUES = {
    "first_name": "Fake", "email": "person@example.com", "resume": RESUME_PDF, "question_1": ("1",), "race": ("9",),
    "demographic_7": ("1",), "data_compliance[gdpr][requires_consent]": "yes", "school": ("1",),
}  # fmt: skip


def test_a_greenhouse_form_as_it_is_really_drawn():
    form, _ = greenhouse.parse(GREENHOUSE_REPLY)
    passed, changes = compared(lines_of(form, GREENHOUSE_VALUES), GREENHOUSE_DRAWN, greenhouse.DRAWN)
    assert keys(passed) == ["first_name", "email", "resume", "question_1"]
    assert fills.pack(passed, DOCUMENTS) == [
        {"key": "first_name", "kind": "text", "value": "Fake"},
        {"key": "email", "kind": "text", "value": "person@example.com"},
        {"key": "resume", "kind": "file", "value": {"slot": "resume"}},
        {"key": "question_1", "kind": "single", "value": "Yes"},
    ]
    assert said(changes) == {
        "Resume/CV": FILE_UNCHECKED,
        "Need sponsorship?": CHOICES_UNCHECKED,
        "Race": UNCHECKED,
        "Pronouns": UNCHECKED,
        "Consent to the privacy notice (GDPR)": UNCHECKED,
        "School": UNCHECKED,
        "Degree": UNCHECKED,
        "Discipline": UNCHECKED,
        "Country*": UNCHECKED,  # as the page words it, like anything the saved read doesn't have
        "Are you Hispanic or Latino?": UNCHECKED,
    }


def test_greenhouse_s_undrawn_fields_don_t_count_as_a_match():
    form, _ = greenhouse.parse(GREENHOUSE_REPLY)
    drawn_only_unlisted = [box("country", "Country*", "single", True)]
    passed, changes = compared(lines_of(form, GREENHOUSE_VALUES), drawn_only_unlisted, greenhouse.DRAWN)
    assert passed == []
    assert said(changes)["First Name"] == GONE and said(changes)["Race"] == UNCHECKED


def test_a_lever_form_as_it_is_really_drawn():
    form, _ = lever.parse(LEVER_PAGE)
    values = {
        "resume": RESUME_PDF, "name": "Fake Person", "email": "person@example.com",
        "urls[LinkedIn]": "https://linkedin.example/fake", "cards[c-1][field2]": ("Yes",),
        "cards[c-1][field3]": ("Robotics", "Search"),
    }  # fmt: skip
    boxes = [
        box("resume", "Resume/CV ✱", "file"),
        box("name", "Full name ✱", required=True),
        box("email", "Email ✱", required=True),
        box("urls[LinkedIn]", "LinkedIn URL"),
        box("cards[c-1][field2]", "Need sponsorship?", "single", True, choices=offered("Yes", "No")),
        box("cards[c-1][field3]", "Which teams?", "multi", choices=offered("Robotics", "Search")),
    ]
    passed, changes = compared(lines_of(form, values), boxes)
    assert fills.pack(passed, DOCUMENTS) == [
        {"key": "resume", "kind": "file", "value": {"slot": "resume"}},
        {"key": "name", "kind": "text", "value": "Fake Person"},
        {"key": "email", "kind": "text", "value": "person@example.com"},
        {"key": "urls[LinkedIn]", "kind": "text", "value": "https://linkedin.example/fake"},
        {"key": "cards[c-1][field2]", "kind": "single", "value": "Yes"},
        {"key": "cards[c-1][field3]", "kind": "multi", "value": ["Robotics", "Search"]},
    ]
    assert said(changes)["Resume/CV"] == FILE_UNCHECKED
    assert said(changes)["Phone"] == GONE  # Lever draws everything it has
    assert "selectedLocation" not in said(changes) and "origin" not in said(changes)  # hidden: never looked at


def test_an_ashby_form_as_it_is_really_drawn():
    form = Form((
        Field("_systemfield_name", "Name", "text", True, "standard"),
        Field("_systemfield_resume", "Resume", "file", True, "standard"),
    ))  # fmt: skip
    boxes = [box("_systemfield_name", "Name*", required=True), box("_systemfield_resume", "Upload File", "file")]
    values = {"_systemfield_name": "Fake Person", "_systemfield_resume": RESUME_PDF}
    passed, changes = compared(lines_of(form, values), boxes, ashby.DRAWN)
    assert keys(passed) == ["_systemfield_name", "_systemfield_resume"]
    assert said(changes) == {"Resume": FILE_UNCHECKED}


# One field at a time

FOUR = Form((
    Field("name", "Full name", "text", True, "standard"),
    Field("why", "Why Acme?", "long_text", False, "company"),
    Field("sponsor", "Need sponsorship?", "single", True, "company", choices=(Choice("Yes", "1"), Choice("No", "0"))),
    Field("teams", "Which teams?", "multi", False, "company", choices=(Choice("Robotics", "r"), Choice("Search", "s"))),
))  # fmt: skip
FOUR_VALUES = {"name": "Fake Person", "why": "Fake words", "sponsor": ("0",), "teams": ("s",)}
FOUR_DRAWN = {
    "name": box("name", "Full name", required=True),
    "why": box("why", "Why Acme?", "long_text"),
    "sponsor": box("sponsor", "Need sponsorship?", "single", True, choices=offered("Yes", "No")),
    "teams": box("teams", "Which teams?", "multi", choices=offered("Robotics", "Search")),
}


def test_a_form_that_is_the_same_passes_whole():
    passed, changes = compared(lines_of(FOUR, FOUR_VALUES), list(FOUR_DRAWN.values()))
    assert keys(passed) == list(FOUR_DRAWN) and changes == []
    assert fills.pack(passed, {}) == [
        {"key": "name", "kind": "text", "value": "Fake Person"},
        {"key": "why", "kind": "long_text", "value": "Fake words"},
        {"key": "sponsor", "kind": "single", "value": "No"},
        {"key": "teams", "kind": "multi", "value": ["Search"]},
    ]


@pytest.mark.parametrize(
    ("key", "change", "question", "what"),
    [
        ("why", {"label": "Why us?"}, "Why Acme?", WORDS),
        ("why", {"kind": "text"}, "Why Acme?", KIND),
        ("why", {"required": True}, "Why Acme?", REQUIRED),
        ("sponsor", {"choices": offered("Yes", "No", "Maybe")}, "Need sponsorship?", CHOICES),
        ("sponsor", {"choices": offered("Yes")}, "Need sponsorship?", CHOICES),
    ],
)
def test_one_difference_leaves_out_that_field_alone(key, change, question, what):
    boxes = FOUR_DRAWN | {key: FOUR_DRAWN[key] | change}
    passed, changes = compared(lines_of(FOUR, FOUR_VALUES), list(boxes.values()))
    assert keys(passed) == [name for name in FOUR_DRAWN if name != key]
    assert changes == [{"question": question, "what": what}]


def test_a_field_gone_from_the_form_leaves_out_that_field_alone():
    boxes = [item for name, item in FOUR_DRAWN.items() if name != "why"]
    passed, changes = compared(lines_of(FOUR, FOUR_VALUES), boxes)
    assert keys(passed) == ["name", "sponsor", "teams"]
    assert changes == [{"question": "Why Acme?", "what": GONE}]


def test_a_field_added_to_the_form_changes_nothing_else():
    passed, changes = compared(lines_of(FOUR, FOUR_VALUES), [*FOUR_DRAWN.values(), box("extra", "Anything else?")])
    assert keys(passed) == list(FOUR_DRAWN)
    assert changes == [{"question": "Anything else?", "what": NEW}]


def test_each_difference_of_one_field_is_said():
    boxes = FOUR_DRAWN | {"why": box("why", "Why us?", "text", True)}
    _, changes = compared(lines_of(FOUR, FOUR_VALUES), list(boxes.values()))
    assert [change["what"] for change in changes] == [WORDS, KIND, REQUIRED]


def test_a_saved_box_that_is_a_file_is_compared_by_key_and_kind_only():
    form = Form((Field("resume", "Resume/CV", "file", True, "standard"),))
    lines = lines_of(form, {"resume": RESUME_PDF})
    assert keys(compared(lines, [box("resume", "Attach", "file")])[0]) == ["resume"]
    passed, changes = compared(lines, [box("resume", "Attach", "text")])
    assert passed == [] and said(changes) == {"Resume/CV": KIND}


def test_a_drawn_box_on_a_saved_hidden_field_is_not_new():
    form = Form((FOUR.fields[0], Field("token", "Token", "hidden", False, "standard")))
    passed, changes = compared(lines_of(form, {"name": "Fake"}), [FOUR_DRAWN["name"], box("token", "Token")])
    assert keys(passed) == ["name"] and changes == []


# Choices


def test_choices_in_another_order_or_capitals_pass():
    boxes = FOUR_DRAWN | {"sponsor": FOUR_DRAWN["sponsor"] | {"choices": offered("NO", "yes")}}
    passed, changes = compared(lines_of(FOUR, FOUR_VALUES), list(boxes.values()))
    assert "sponsor" in keys(passed) and changes == []


def test_the_pack_holds_the_label_as_drawn():
    boxes = FOUR_DRAWN | {
        "sponsor": FOUR_DRAWN["sponsor"] | {"choices": offered("YES", "NO!")},
        "teams": FOUR_DRAWN["teams"] | {"choices": offered("robotics", "search")},
    }
    passed, _ = compared(lines_of(FOUR, FOUR_VALUES), list(boxes.values()))
    values = {item["key"]: item["value"] for item in fills.pack(passed, {})}
    assert (values["sponsor"], values["teams"]) == ("NO!", ["search"])


def test_no_drawn_choices_is_a_change_on_lever_and_ashby():
    boxes = FOUR_DRAWN | {"sponsor": box("sponsor", "Need sponsorship?", "single", True)}
    for board in (lever.DRAWN, ashby.DRAWN):
        passed, changes = compared(lines_of(FOUR, FOUR_VALUES), list(boxes.values()), board)
        assert "sponsor" not in keys(passed) and said(changes) == {"Need sponsorship?": CHOICES}


def test_no_drawn_choices_passes_on_greenhouse_and_says_so():
    boxes = FOUR_DRAWN | {
        "sponsor": box("sponsor", "Need sponsorship?", "single", True),
        "teams": box("teams", "Which teams?", "multi"),
    }
    passed, changes = compared(lines_of(FOUR, FOUR_VALUES), list(boxes.values()), greenhouse.DRAWN)
    assert keys(passed) == list(FOUR_DRAWN)
    assert said(changes) == {"Need sponsorship?": CHOICES_UNCHECKED, "Which teams?": CHOICES_UNCHECKED}
    values = {item["key"]: item["value"] for item in fills.pack(passed, {})}
    assert (values["sponsor"], values["teams"]) == ("No", ["Search"])  # the saved label, as nothing else is drawn


def test_other_drawn_choices_are_still_a_change_on_greenhouse():
    boxes = FOUR_DRAWN | {"sponsor": FOUR_DRAWN["sponsor"] | {"choices": offered("Maybe")}}
    passed, changes = compared(lines_of(FOUR, FOUR_VALUES), list(boxes.values()), greenhouse.DRAWN)
    assert "sponsor" not in keys(passed) and said(changes) == {"Need sponsorship?": CHOICES}


@pytest.mark.parametrize(
    ("drawn_as", "what"),
    [
        (box("race", "Race", "single", choices=offered("Decline")), None),
        (box("race", "Your race", "single", choices=offered("Decline")), WORDS),
        (box("race", "Race", "multi", choices=offered("Decline")), KIND),
    ],
)
def test_a_saved_key_that_may_not_be_drawn_is_compared_when_it_is(drawn_as, what):
    form = Form((Field("race", "Race", "single", False, "eeo", choices=(Choice("Decline", "9"),)),))
    passed, changes = compared(lines_of(form, {"race": ("9",)}), [drawn_as], greenhouse.DRAWN)
    assert keys(passed) == ([] if what else ["race"])
    assert said(changes) == ({"Race": what} if what else {})


def test_a_key_beginning_like_an_undrawn_one_is_said_unchecked():
    form = Form((FOUR.fields[0], Field("demographic_12", "Age", "single", False, "demographic")))
    _, changes = compared(lines_of(form, {"name": "Fake"}), [FOUR_DRAWN["name"]], greenhouse.DRAWN)
    assert said(changes) == {"Age": UNCHECKED}


# The pack


@pytest.mark.parametrize("status", ["suggested", "decide", "left", "empty", "write", "hidden"])
def test_only_lines_to_fill_go_out(status):
    field = FOUR.fields[0]
    passed, changes = compared((match.Line(field, status, "Fake draft"),), [FOUR_DRAWN["name"]])
    assert keys(passed) == ["name"] and changes == []  # it still counts as a match
    assert fills.pack(passed, {}) == []


def test_a_value_in_the_body_never_reaches_the_pack():
    boxes = [FOUR_DRAWN["name"] | {"value": "from the page"}, FOUR_DRAWN["why"] | {"answer": "from the page"}]
    passed, _ = compared(lines_of(FOUR, FOUR_VALUES), boxes)
    assert "from the page" not in json.dumps(fills.pack(passed, {}))


def test_a_pack_never_holds_a_path():
    form = Form((
        Field("resume", "Resume/CV", "file", True, "standard"),
        Field("cover_letter", "Cover Letter", "file", False, "standard"),
    ))  # fmt: skip
    letter = "/fake/folder/letter-v1.pdf"
    lines = lines_of(form, {"resume": RESUME_PDF, "cover_letter": letter})
    passed, _ = compared(lines, [box("resume", "Attach", "file"), box("cover_letter", "Attach", "file")])
    both = DOCUMENTS | {"cover_letter": ("Cover letter v1", Path(letter))}
    found = fills.pack(passed, both)
    assert [item["value"] for item in found] == [{"slot": "resume"}, {"slot": "cover_letter"}]
    assert "/fake" not in json.dumps(found)


def test_a_file_that_is_no_document_stays_out():
    form = Form((Field("resume", "Resume/CV", "file", True, "standard"),))
    passed, _ = compared(lines_of(form, {"resume": "/fake/folder/other.pdf"}), [box("resume", "Attach", "file")])
    assert fills.pack(passed, DOCUMENTS) == []


def test_a_consent_is_a_tick_and_nothing_else_is():
    form = Form((Field("consent[a]", "I agree", "consent", False, "privacy"),))
    drawn_box = [box("consent[a]", "I agree", "consent")]
    passed, _ = compared(lines_of(form, {"consent[a]": match.TICKED}), drawn_box)
    assert fills.pack(passed, {}) == [{"key": "consent[a]", "kind": "consent", "value": "yes"}]
    passed, _ = compared(lines_of(form, {"consent[a]": "no thanks"}), drawn_box)
    assert fills.pack(passed, {}) == []


# A hostile page


@pytest.mark.parametrize(
    "item",
    [
        None, 1, "email", [GOOD_BOX],
        {"label": "Email", "kind": "text", "required": True},
        {"key": "email", "kind": "text", "required": True},
        {"key": "email", "label": "Email", "required": True},
        {"key": "email", "label": "Email", "kind": "text"},
        GOOD_BOX | {"key": ""}, GOOD_BOX | {"key": 1}, GOOD_BOX | {"key": None}, GOOD_BOX | {"key": ["email"]},
        GOOD_BOX | {"key": "x" * 1001},
        GOOD_BOX | {"label": 1}, GOOD_BOX | {"label": None}, GOOD_BOX | {"label": ["Email"]},
        GOOD_BOX | {"kind": "dropdown"}, GOOD_BOX | {"kind": 1}, GOOD_BOX | {"kind": None},
        GOOD_BOX | {"kind": ["text"]}, GOOD_BOX | {"kind": {"a": 1}},
        GOOD_BOX | {"required": 1}, GOOD_BOX | {"required": "true"}, GOOD_BOX | {"required": None},
        GOOD_BOX | {"choices": None}, GOOD_BOX | {"choices": "Yes"}, GOOD_BOX | {"choices": {"label": "Yes"}},
        GOOD_BOX | {"choices": ["Yes"]}, GOOD_BOX | {"choices": [None]}, GOOD_BOX | {"choices": [{"label": "Yes"}]},
        GOOD_BOX | {"choices": [{"value": "1"}]}, GOOD_BOX | {"choices": [{"label": 1, "value": "1"}]},
        GOOD_BOX | {"choices": [{"label": "Yes", "value": 1}]},
        GOOD_BOX | {"accept": None}, GOOD_BOX | {"accept": ".pdf"}, GOOD_BOX | {"accept": [1]},
    ],
)  # fmt: skip
def test_anything_wrong_in_a_drawn_field_refuses_the_whole_list(item):
    with pytest.raises(ValueError):  # noqa: PT011 (the only thing the route needs to know)
        fills.drawn([GOOD_BOX, item])


def test_every_kind_of_box_is_known():
    kinds = ("text", "long_text", "single", "multi", "file", "location", "consent", "unknown")
    found = fills.drawn([box(kind, "Label", kind) for kind in kinds])
    assert [field.kind for field in found] == list(kinds)


def test_a_key_of_1000_characters_is_read_and_help_is_not():
    (found,) = fills.drawn([GOOD_BOX | {"key": "x" * 1000, "help": 1}])
    assert found.key == "x" * 1000 and found.help == ""


def test_choices_and_file_types_may_be_left_out_or_given():
    (found,) = fills.drawn([GOOD_BOX | {"choices": offered("Yes"), "accept": [".pdf"]}])
    assert found.choices == (Choice("Yes", "site-0"),) and found.accept == (".pdf",)


def test_hidden_boxes_are_dropped_and_a_repeated_key_keeps_the_first():
    found = fills.drawn([GOOD_BOX, GOOD_BOX | {"label": "Second"}, box("token", "Token", "hidden")])
    assert [(field.key, field.label) for field in found] == [("email", "Email")]


def test_a_long_label_is_cut_in_what_is_said():
    _, changes = compared(lines_of(Form(()), {}), [box("extra", "x" * 2000)])
    assert changes == [{"question": "x" * 1000, "what": NEW}]


def test_invisible_characters_are_taken_out_of_a_drawn_label():
    (found,) = fills.drawn([box("extra", "Fake\x00 \x07label\n")])
    assert found.label == "Fake label"


@pytest.mark.parametrize(("extra", "more"), [(100, None), (101, 1), (150, 50)])
def test_at_most_100_changes_and_then_how_many_more(extra, more):
    _, changes = compared(lines_of(Form(()), {}), [box(f"key_{number}", f"Label {number}") for number in range(extra)])
    assert len(changes) == (100 if more is None else 101)
    assert changes[99] == {"question": "Label 99", "what": NEW}
    if more:
        assert changes[100] == {"question": f"{more:,} more", "what": "not shown here"}


# Who may read `changes`


def test_only_these_files_name_the_fills_table_and_fills_py_logs_nothing():
    root = Path(fills.__file__).parents[1]
    naming = re.compile(r"\b(?:FROM|INTO|UPDATE|JOIN|TABLE)\s+fills\b")
    found = {str(path.relative_to(root)) for path in root.rglob("*.py") if naming.search(path.read_text("utf-8"))}
    allowed = {"db.py", "submit/fills.py", "web/plugin.py", "web/plugin_settings.py", "web/submit.py"}
    assert "submit/fills.py" in found and found <= allowed
    source = Path(fills.__file__).read_text("utf-8")
    assert not re.search(r"\bprint\(|\blogging\b|\blogger\b", source)
