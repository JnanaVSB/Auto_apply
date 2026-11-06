"""Fills: the form's address, the rules for one, the row that counts it and its ticket, with a fake clock."""

import inspect
import re
from contextlib import closing, contextmanager
from pathlib import Path

import pytest
import sqlcipher3
from helpers import add_job, one
from pydantic import ValidationError

from app import db
from app.config import Config
from app.sources import closed_site, is_simplify_site, posting_of
from app.submit import Drawn, ashby, fill_words, fills, greenhouse, lever
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
    assert lever.DRAWN == ashby.DRAWN == Drawn()


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


def test_presses_on_a_ticket_nobody_took_leave_one_waiting_row(conn, laptop, at, tickets):
    found = job(conn)
    for _ in range(3):
        assert tickets.make(conn, found, READY, LIMITS) is None
    assert rows(conn) == 1
    assert one(conn, ROW, only(tickets).fill)[4:] == ("waiting", None)


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
    running, toolbar, button = three_fills(conn, tickets, laptop)
    with conn:
        tickets.end_all(conn)
    assert not tickets.tickets
    for ticket in (running, toolbar):
        assert one(conn, ROW, ticket.fill)[4:] == ("ended", "browser-removed")
    assert one(conn, ROW, button.fill)[4:] == ("ended", "logged-out")  # it never had a browser to remove


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


@pytest.mark.parametrize("end", ["end_browser", "end_all", "end_job"])
def test_an_ending_keeps_a_filled_row_and_what_was_filled(conn, laptop, at, tickets, end):
    running, *_ = three_fills(conn, tickets, laptop)
    tickets.save_answer(conn, running, [])
    running.questions = {"first_name": "First Name"}
    tickets.report(conn, running, None, ["first_name"], {})
    with conn:
        getattr(tickets, end)(conn, *{"end_browser": (laptop,), "end_all": (), "end_job": (running.job,)}[end])
    kind = "submitted" if end == "end_job" else "browser-removed"
    assert one(conn, ROW, running.fill)[4:] == ("filled", kind)
    assert fill_words.latest(conn, running.job)["filled"] == ["First Name"]


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


# Who may read `changes`


def files_holding(pattern):
    root = Path(fills.__file__).parents[1]
    return {str(path.relative_to(root)) for path in root.rglob("*.py") if pattern.search(path.read_text("utf-8"))}


def test_only_these_files_name_the_fills_table():
    found = files_holding(re.compile(r"\b(?:FROM|INTO|UPDATE|JOIN|TABLE)\s+fills\b"))
    allowed = {
        "db.py",
        "submit/fill_words.py",
        "submit/fills.py",
        "web/plugin.py",
        "web/plugin_settings.py",
        "web/submit.py",
    }
    assert "submit/fills.py" in found and found <= allowed


def test_only_the_box_and_settings_read_a_fill_back():
    reads = r"\bfill_words\.(?:latest|recent)\b|\bfrom app\.submit\.fill_words import[^)]*\b(?:latest|recent)\b"
    found = files_holding(re.compile(reads))
    assert found == {"web/plugin_settings.py", "web/submit.py"}


def test_nothing_that_holds_a_fill_s_changes_logs():
    holding = {
        "submit/compare.py",
        "submit/fill_words.py",
        "submit/fills.py",
        "web/plugin.py",
        "web/plugin_settings.py",
        "web/submit.py",
    }
    logs = files_holding(re.compile(r"\bprint\(|\blogging\b|\blogger\b"))
    assert not logs & holding
