# ruff: noqa: F811 (either, phone_dashboard and serve are fixtures from test_web_phone.py and test_web_plugin.py)
"""Do this on a computer: the phone's mark on a job, and Ready's list of the marked jobs. Made-up jobs only."""

import pytest
from helpers import add_job, csrf, log_in, one
from test_web_phone import EXPIRED, ORIGIN, either, phone_login, serve, words  # noqa: F401
from test_web_plugin import phone_dashboard  # noqa: F401

from app.web import actions, queries

MARK, TAKE_OFF, ON_LIST, LIST = "Do this on a computer", "Take it off", "On the computer list", "To do on a computer"
NO_FORM = "https://careers.example/1"  # a site the app reads no form from: the mark is there all the same
RECORD = """INSERT INTO submissions (job_id, submitted_on, saved_on, how, address, fields, files)
    VALUES (?, '2026-11-05', 'then', 'by hand', 'https://careers.example/1', '[]', '[]')"""
MARKED = "SELECT for_computer FROM jobs"
TRACKER = "INSERT INTO tracker (job_id, stage, applied_on, changed_on) VALUES (?, ?, ?, 'then')"


@pytest.fixture
def both(phone_dashboard):
    """The laptop and the phone, each logged in; each one's headers and token ride along with its presses."""
    laptop, phone = phone_dashboard
    log_in(laptop)
    phone.cookie, session = phone_login(phone)
    laptop.sent, laptop.token = {}, csrf(laptop)
    phone.sent, phone.token = phone.cookie | ORIGIN, session.csrf
    return laptop, phone


def on_phone(phone, path):
    return words(phone.get(path, headers=phone.cookie))


def press(client, job, value, **form):
    """The mark's button, from the laptop or the phone."""
    data = form | {"value": value, "csrf_token": client.token}
    return client.post(f"/jobs/{job}/form/computer", data=data, headers=client.sent, follow_redirects=False)


def test_the_phone_marks_a_job_and_takes_it_off(both, conn):
    _, phone = both
    with conn:
        job = add_job(conn, "ML Intern", apply_url=NO_FORM)
    part = on_phone(phone, f"/jobs/{job}/form")
    assert MARK in part and ON_LIST not in part
    for _ in range(2):  # pressing twice leaves it on
        part = press(phone, job, "1")
        assert part.status_code == 200
        assert ON_LIST in words(part) and TAKE_OFF in words(part) and MARK not in words(part)
        assert one(conn, MARKED) == (1,)
    assert ON_LIST in on_phone(phone, f"/jobs/{job}/form")  # saved, not only in the answer to the press
    for _ in range(2):  # taking it off twice leaves it off
        assert MARK in words(press(phone, job, "0"))
        assert one(conn, MARKED) == (0,)


def test_a_recorded_job_shows_the_mark_nowhere_and_keeps_it(both, conn):
    laptop, phone = both
    with conn:
        job = add_job(conn, "ML Intern", apply_url=NO_FORM, for_computer=1)
        conn.execute(RECORD, (job,))
    part = on_phone(phone, f"/jobs/{job}/form")
    assert MARK not in part and ON_LIST not in part
    assert LIST not in words(laptop.get("/ready")) and LIST not in on_phone(phone, "/ready")
    assert one(conn, MARKED) == (1,)  # unseen, but left in the row


@pytest.mark.parametrize(("stage", "listed"), [("ready", True), ("applied", False), ("rejected", False)])
def test_a_card_moved_on_from_ready_on_the_tracker_leaves_the_list_and_the_button(both, conn, stage, listed):
    laptop, phone = both
    with conn:
        job = add_job(conn, "ML Intern", apply_url=NO_FORM, for_computer=1)
        conn.execute(TRACKER, (job, stage, None if stage == "ready" else "2026-11-01"))  # by hand, or by Gmail
    assert (LIST in words(laptop.get("/ready")), LIST in on_phone(phone, "/ready")) == (listed, listed)
    assert (ON_LIST in on_phone(phone, f"/jobs/{job}/form")) == listed
    assert one(conn, MARKED) == (1,)  # unseen, but left in the row


@pytest.mark.parametrize("marked", [0, 1], ids=["not marked", "marked"])
@pytest.mark.parametrize(
    "change",
    [{"status": "closed"}, {"status": "no longer available"}, {"not_interested": 1}],
    ids=["closed", "gone", "skipped"],
)
def test_the_phone_offers_no_mark_on_a_job_the_list_leaves_out(both, conn, change, marked):
    _, phone = both
    with conn:
        job = add_job(conn, "ML Intern", apply_url=NO_FORM, for_computer=marked, **change)
    part = on_phone(phone, f"/jobs/{job}/form")
    assert MARK not in part and ON_LIST not in part


@pytest.mark.parametrize("marked", [0, 1], ids=["not marked", "marked"])
def test_the_laptop_job_box_never_shows_the_mark(either, conn, marked):
    log_in(either)
    with conn:
        job = add_job(conn, "ML Intern", apply_url=NO_FORM, for_computer=marked)
    part = words(either.get(f"/jobs/{job}/form"))
    assert "form/computer" not in part and ON_LIST not in part


def test_ready_lists_the_marked_jobs_still_to_do_on_both_addresses(both, conn):
    laptop, phone = both
    assert LIST not in words(laptop.get("/ready"))  # nothing marked: no list at all
    with conn:
        job = add_job(conn, "ML Intern", company="Initech", for_computer=1)
        add_job(conn, "Skipped Intern", for_computer=1, not_interested=1)
        add_job(conn, "Closed Intern", for_computer=1, status="closed")
        add_job(conn, "Gone Intern", for_computer=1, status="no longer available")
        add_job(conn, "Unmarked Intern")
        newer = add_job(conn, "Robotics Intern", company="Hooli", for_computer=1)
    for page in (words(laptop.get("/ready")), on_phone(phone, "/ready")):
        listed = page.split(f"<h2>{LIST}</h2>")[1].split('class="summary"')[0]  # above the count
        assert f'<a href="/jobs/{job}">ML Intern</a>' in listed and "Initech" in listed
        assert f'action="/jobs/{job}/form/computer"' in listed and TAKE_OFF in listed
        assert f'<a href="/jobs/{newer}">Robotics Intern</a>' in listed
        assert listed.index("Robotics Intern") < listed.index("ML Intern")  # newest first
        assert listed.count("<li>") == 2  # skipped, closed, gone and unmarked jobs left out


def test_take_it_off_on_ready_works_from_both_addresses(both, conn):
    with conn:
        job = add_job(conn, "ML Intern", for_computer=1)
    for client in both:
        with conn:
            conn.execute("UPDATE jobs SET for_computer = 1")
        response = press(client, job, "0", to="ready")
        assert (response.status_code, response.headers["location"]) == (303, "/ready")
        assert one(conn, MARKED) == (0,)


def test_the_mark_needs_a_login_the_token_and_this_site(both, conn):
    _, phone = both
    with conn:
        job = add_job(conn, "ML Intern", for_computer=1)
    path, data = f"/jobs/{job}/form/computer", {"value": "0"}
    no_login = phone.post(path, data=data, headers=ORIGIN, follow_redirects=False)
    assert (no_login.status_code, no_login.headers["location"]) == (303, "/login")
    for token in ({}, {"csrf_token": "made-up-token"}):
        refused = phone.post(path, data=data | token, headers=phone.cookie | ORIGIN, follow_redirects=False)
        assert (refused.status_code, EXPIRED in words(refused)) == (403, True)
    another_site = phone.cookie | {"Origin": "https://attacker.example"}
    sent = phone.post(path, data=data | {"csrf_token": phone.token}, headers=another_site, follow_redirects=False)
    assert sent.status_code == 403
    assert one(conn, MARKED) == (1,)  # untouched by every refused press


@pytest.mark.parametrize("value", ["", "2", "yes"])
def test_only_1_sets_the_mark_any_other_value_takes_it_off(both, conn, value):
    _, phone = both
    with conn:
        job = add_job(conn, "ML Intern", apply_url=NO_FORM, for_computer=1)
    assert MARK in words(press(phone, job, value))
    assert one(conn, MARKED) == (0,)


def test_an_unknown_job_is_not_found_from_either_address(both):
    for client in both:
        assert press(client, 999, "1").status_code == 404


def test_a_title_on_ready_shows_as_text(both, conn):
    laptop, _ = both
    with conn:
        add_job(conn, "<script>alert(1)</script>", for_computer=1)
    page = laptop.get("/ready").text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "<script>alert" not in page


def test_set_for_computer_says_whether_there_is_such_a_job(conn):
    with conn:
        job = add_job(conn, "ML Intern")
    assert actions.set_for_computer(conn, job, True) and not actions.set_for_computer(conn, job + 1, True)
    assert one(conn, MARKED) == (1,)


def test_the_list_reads_only_the_marked_jobs(conn):
    plan = conn.execute(f"EXPLAIN QUERY PLAN {queries.FOR_COMPUTER}").fetchall()
    assert "USING INDEX jobs_for_computer" in plan[0][3]
