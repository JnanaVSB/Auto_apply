# ruff: noqa: F811 (door, limits and phone_dashboard are fixtures from test_web_plugin.py)
"""The Open and fill button, its press, the job box's line about the last fill and Settings' Recent fills, with made-up
jobs, browsers and forms. Nothing here opens a site."""

import html
import json
from datetime import date

import pytest
from helpers import add_job, csrf, log_in, one
from test_web_phone import ORIGIN, phone_login
from test_web_plugin import (
    FORM_ADDRESS,
    LINK,
    NAME_BOX,
    NAME_PACK,
    PLUGIN,
    REMOVE,
    SETTINGS,
    START,
    call,
    door,  # noqa: F401
    fill_rows,
    held,
    limits,  # noqa: F401
    pair,
    phone_dashboard,  # noqa: F401
    ready_job,
    refused,
    send,
)

from app import config, submit
from app.submit import Field, Form, fills
from app.web import actions
from app.web import submit as form_part

NEEDS_A_FILE = Form((Field("resume", "Resume/CV", "file", True, "standard"),))
ADD_FILL = """INSERT INTO fills (job_id, address, started_on, toolbar, browser, state, stop_kind, changes)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)"""
HONEST = (
    "Filling shares these answers and your PDF with the site, even before you press Submit. Other plugins in this "
    "browser can read them too."
)
NOT_FILLED = "The form opened without filling."
FILLING = "Filling in the new tab…"
OPEN_FIRST = "Answer the decisions above first, then Open and fill appears."
CONNECT_FIRST = "Connect a browser first: Settings → Browser plugin."
RECORD = """INSERT INTO submissions (job_id, submitted_on, saved_on, how, address, fields, files)
    VALUES (?, '2026-11-05', 'then', 'by hand', ?, '[]', '[]')"""
ON_BOARD = "INSERT INTO tracker VALUES (?, 'applied', NULL, '2026-10-17', 'then')"
STARTED_ON = "2026-11-05T21:02:00+00:00"  # 14:02 in Phoenix
WORDS = {
    ("waiting", None): "no browser has opened it yet",
    ("started", None): "opened in a browser",
    ("sent", None): "your answers went to the browser",
    ("stopped", "unexpected-page"): (
        "stopped: the page didn't look like this job's form, so nothing was filled. Check the tab, then press Open and "
        "fill again."
    ),
    ("stopped", "too-many-fields"): "stopped: the page had more boxes than a form has, so nothing was filled.",
    ("ended", "browser-removed"): "ended when its browser was removed",
    ("ended", "replaced"): "ended by a newer press",
    ("ended", "submitted"): "ended when you pressed I submitted it",
}


@pytest.fixture
def client(client, limits):
    log_in(client)
    return client


@pytest.fixture
def paired(conn):
    return pair(conn, plugin=PLUGIN)


def box(client, job):
    return client.get(f"/jobs/{job}/form").text


def press(client, job):
    return client.post(f"/jobs/{job}/form/fill", headers={"X-CSRF-Token": csrf(client)})


def status(words):
    return f'<p id="fill-status" role="status" hx-swap-oob="true">{html.escape(words, quote=False)}</p>'


def button(job):
    address = html.escape(FORM_ADDRESS)
    return (
        f'<span hx-post="/jobs/{job}/form/fill" hx-swap="none"><a href="{address}" target="_blank" rel="noopener">'
        "Open and fill</a></span>"
    )


def past_fill(conn, job, started_on=STARTED_ON, toolbar=0, browser=None, state="sent", stop=None, changes=None):
    with conn:
        conn.execute(ADD_FILL, (job, FORM_ADDRESS, started_on, toolbar, browser, state, stop, changes))


def now_fill(conn, job):
    """A fill made a moment ago, as the cap and the pause see it."""
    past_fill(conn, job, fills.now())


def recent_part(page):
    return page.partition("<h2>Recent fills</h2>")[2].partition("<h2>Add a browser</h2>")[0]


# The button


def test_the_button_is_drawn_as_it_must_be(client, conn, paired):
    job = ready_job(conn)
    page = box(client, job)
    assert button(job) in page and HONEST in page
    assert '<p id="fill-status" role="status"></p>' in page
    assert f'<a href="{LINK}" target="_blank" rel="noopener">Open the posting</a>' in page
    assert f'hx-swap="none"><a href="{LINK}"' not in page  # the plain link never fills
    assert page.count(f'hx-post="/jobs/{job}/form/fill"') == 1


def test_the_button_sits_before_to_copy(client, conn, paired):
    job = ready_job(conn)
    page = box(client, job)
    assert page.index("What would be filled in") < page.index(button(job)) < page.index("<h3>To copy</h3>")


def test_a_form_not_read_shows_no_button(client, conn, paired):
    with conn:
        job = add_job(conn, "ML Intern", apply_url=LINK)
    page = box(client, job)
    assert "form/fill" not in page and "fill-status" not in page


def test_a_job_with_a_record_shows_no_button(client, conn, paired):
    job = ready_job(conn)
    with conn:
        conn.execute(RECORD, (job, FORM_ADDRESS))
    page = box(client, job)
    assert "form/fill" not in page and "fill-status" not in page


def test_an_open_decision_takes_the_button_s_place(client, conn, paired):
    with conn:
        job = add_job(conn, "ML Intern", apply_url=LINK)
    submit.save(conn, job, NEEDS_A_FILE, "api")
    page = box(client, job)
    assert "form/fill" not in page and OPEN_FIRST in page


def test_no_connected_browser_takes_the_button_s_place(client, conn):
    pair(conn)  # a key never pasted
    page = box(client, ready_job(conn))
    assert "form/fill" not in page and CONNECT_FIRST in page


def test_the_cap_takes_the_button_s_place(client, conn, paired, limits):
    limits(cap=1)
    job = ready_job(conn)
    now_fill(conn, add_job(conn, "Other"))
    page = box(client, job)
    assert "form/fill" not in page
    assert "Today&#39;s 1 fill is used: more tomorrow, or raise the number in Settings." in page


def test_the_pause_takes_the_button_s_place(client, conn, paired, limits):
    limits(pause=2)
    job = ready_job(conn)
    now_fill(conn, add_job(conn, "Other"))
    page = box(client, job)
    assert "form/fill" not in page and "The next fill can start in 2 minutes" in page


def test_an_ashby_job_with_no_read_shows_no_button(client, conn, paired):
    with conn:
        job = add_job(conn, "ML Intern", apply_url="https://jobs.ashbyhq.com/acme/0a1b-2c3d")
    assert "form/fill" not in box(client, job)


def test_a_job_on_another_site_shows_no_button(client, conn, paired):
    with conn:
        job = add_job(conn, "ML Intern", apply_url="https://careers.example/1")
    page = box(client, job)
    assert "form/fill" not in page and "fill-status" not in page


def test_settings_that_can_t_be_read_are_said_and_the_box_still_draws(client, conn, paired, tmp_path):
    job = ready_job(conn)
    (tmp_path / "config.yaml").unlink()
    page = box(client, job)
    assert "form/fill" not in page and "Settings file not found" in page and "<h3>To copy</h3>" in page


# The press


def test_a_press_makes_a_fill_and_answers_one_line_only(client, conn, paired):
    job = ready_job(conn)
    response = press(client, job)
    assert response.status_code == 200 and response.text.strip() == status(FILLING)
    assert fill_rows(conn) == [(0, None, "waiting", None)]
    (ticket,) = held(client).values()
    assert (ticket.job, ticket.address, ticket.browser) == (job, FORM_ADDRESS, None)


def test_a_press_needs_a_login(client, door, conn, paired):
    job = ready_job(conn)
    response = door.post(f"/jobs/{job}/form/fill", follow_redirects=False)
    assert response.status_code == 303 and fill_rows(conn) == []


def test_a_press_needs_the_anti_forgery_token(client, conn, paired):
    job = ready_job(conn)
    assert client.post(f"/jobs/{job}/form/fill").status_code == 403
    assert fill_rows(conn) == []


def test_a_press_on_no_such_job_is_not_found(client, conn, paired):
    assert press(client, 999).status_code == 404


@pytest.mark.parametrize(
    ("setup", "words"),
    [
        ("record", NOT_FILLED),
        ("no form", NOT_FILLED),
        ("open", f"{OPEN_FIRST} {NOT_FILLED}"),
        ("no browser", f"{CONNECT_FIRST} {NOT_FILLED}"),
    ],
)
def test_a_refused_press_says_why_and_that_the_form_opened_unfilled(client, conn, setup, words):
    if setup != "no browser":
        pair(conn, plugin=PLUGIN)
    if setup in ("no form", "open"):
        with conn:
            job = add_job(conn, "ML Intern", apply_url=LINK)
    else:
        job = ready_job(conn)
    if setup == "open":
        submit.save(conn, job, NEEDS_A_FILE, "api")
    if setup == "record":
        with conn:
            conn.execute(RECORD, (job, FORM_ADDRESS))
    response = press(client, job)
    assert response.status_code == 200 and response.text.strip() == status(words)
    assert fill_rows(conn) == [] and not held(client)


def test_the_cap_and_the_pause_refuse_a_press(client, conn, paired, limits):
    job = ready_job(conn)
    now_fill(conn, add_job(conn, "Other"))
    limits(pause=2)
    assert "The next fill can start in 2 minutes" in press(client, job).text
    limits(cap=1)
    text = press(client, job).text
    assert "Today" in text and NOT_FILLED in text
    assert len(fill_rows(conn)) == 1 and not held(client)


def test_a_busy_database_says_so_and_leaves_no_ticket(client, conn, paired):
    job = ready_job(conn)
    conn.execute("BEGIN IMMEDIATE")
    response = press(client, job)
    conn.rollback()
    assert response.status_code == 200
    assert response.text.strip() == status(f"{actions.BUSY} {NOT_FILLED}")
    assert fill_rows(conn) == [] and not held(client)


def test_settings_that_can_t_be_read_refuse_a_press(client, conn, paired, tmp_path):
    job = ready_job(conn)
    (tmp_path / "config.yaml").unlink()
    response = press(client, job)
    assert response.status_code == 200 and "Settings file not found" in response.text and NOT_FILLED in response.text
    assert fill_rows(conn) == [] and not held(client)


def test_two_quick_presses_can_t_both_slip_under_the_cap(client, conn, paired, limits):
    limits(cap=1)
    first, second = ready_job(conn), ready_job(conn, "https://jobs.lever.co/acme/1", "Second")
    assert FILLING in press(client, first).text
    assert NOT_FILLED in press(client, second).text
    assert len(fill_rows(conn)) == 1 and list(held(client)) == [first]


def test_a_repeat_press_in_the_same_day_makes_no_second_row(client, conn, paired, limits):
    limits(pause=2)
    job = ready_job(conn)
    assert FILLING in press(client, job).text
    assert FILLING in press(client, job).text
    assert len(fill_rows(conn)) == 1


def test_a_press_can_be_used_all_the_way(client, door, conn, paired):
    press(client, ready_job(conn))
    ticket = call(door, START).json()["ticket"]
    assert send(door, ticket, [NAME_BOX]).json() == {"go": True, "pack": [NAME_PACK]}
    assert fill_rows(conn) == [(0, "Laptop Chrome", "sent", None)]


# The phone


def test_the_phone_gets_no_button_and_says_why(phone_dashboard, conn, tmp_path, data_dir, monkeypatch):
    use_settings(tmp_path, data_dir, monkeypatch)
    pair(conn, plugin=PLUGIN)
    job = ready_job(conn)
    laptop, phone = phone_dashboard
    cookie, _ = phone_login(phone)
    page = phone.get(f"/jobs/{job}/form", headers=cookie).text
    assert "form/fill" not in page and form_part.ON_PHONE in page
    log_in(laptop)
    page = laptop.get(f"/jobs/{job}/form").text
    assert button(job) in page and form_part.ON_PHONE not in page


def test_the_phone_says_nothing_of_filling_on_a_job_with_no_form(
    phone_dashboard, conn, tmp_path, data_dir, monkeypatch
):
    use_settings(tmp_path, data_dir, monkeypatch)
    with conn:
        job = add_job(conn, "ML Intern", apply_url="https://careers.example/1")
    _, phone = phone_dashboard
    cookie, _ = phone_login(phone)
    assert form_part.ON_PHONE not in phone.get(f"/jobs/{job}/form", headers=cookie).text


def test_a_press_from_the_phone_is_refused(phone_dashboard, conn, tmp_path, data_dir, monkeypatch):
    use_settings(tmp_path, data_dir, monkeypatch)
    pair(conn, plugin=PLUGIN)
    job = ready_job(conn)
    _, phone = phone_dashboard
    cookie, session = phone_login(phone)
    headers = cookie | ORIGIN | {"X-CSRF-Token": session.csrf}
    response = phone.post(f"/jobs/{job}/form/fill", headers=headers)
    assert response.status_code == 200 and response.text.strip() == status(form_part.ON_PHONE)
    assert fill_rows(conn) == [] and not phone.app.state.tickets.tickets


def use_settings(tmp_path, data_dir, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(f"data_dir: {data_dir}\nfill_pause_minutes: 0\n", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", path)


# "I submitted it"


def test_i_submitted_it_ends_the_fill(client, door, conn, paired):
    job = ready_job(conn)
    with conn:
        conn.execute(ON_BOARD, (job,))
    press(client, job)
    ticket = call(door, START).json()["ticket"]
    data = {"on": date.today().isoformat(), "csrf_token": csrf(client)}
    assert client.post(f"/jobs/{job}/form/submitted", data=data).status_code == 200
    assert fill_rows(conn) == [(0, "Laptop Chrome", "ended", "submitted")]
    assert refused(send(door, ticket, [NAME_BOX]), 404, "no-ticket")


def test_a_refused_i_submitted_it_ends_nothing(client, door, conn, paired):
    job = ready_job(conn)
    press(client, job)
    data = {"on": "not a date", "csrf_token": csrf(client)}
    client.post(f"/jobs/{job}/form/submitted", data=data)
    assert fill_rows(conn) == [(0, None, "waiting", None)] and held(client)


# The box's line about the last fill


@pytest.mark.parametrize(("state", "stop"), list(WORDS))
def test_the_last_fill_says_what_became_of_it(client, conn, paired, phoenix, state, stop):
    job = ready_job(conn)
    past_fill(conn, job, state=state, stop=stop)
    words = WORDS[state, stop]
    line = f"Last fill: 5 Nov 14:02, from Open and fill: {words}{'' if words.endswith('.') else '.'}"
    assert line in html.unescape(box(client, job))


def test_the_last_fill_names_the_toolbar_and_the_browser(client, conn, paired, phoenix):
    job = ready_job(conn)
    past_fill(conn, job, "2026-11-01T00:00:00+00:00")
    past_fill(conn, job, toolbar=1, browser="Laptop Chrome", state="sent")
    page = box(client, job)
    assert "Last fill: 5 Nov 14:02, from the toolbar, in Laptop Chrome: your answers went to the browser." in page
    assert page.count("Last fill:") == 1


def test_an_odd_row_never_breaks_a_page(client, conn, paired):
    job = ready_job(conn)
    past_fill(conn, job, state="a state from elsewhere", stop="an odd kind")
    assert "a state from elsewhere" in box(client, job)
    assert "a state from elsewhere" in recent_part(client.get(SETTINGS).text)


def test_a_job_with_no_fill_has_no_line(client, conn, paired):
    assert "Last fill" not in box(client, ready_job(conn))


def test_the_last_fill_shows_on_a_job_that_can_t_fill_now(client, conn):
    job = ready_job(conn)  # no browser left
    past_fill(conn, job, browser="Old laptop", state="ended", stop="browser-removed")
    assert "ended when its browser was removed" in box(client, job)


def test_what_was_left_for_you_is_listed_and_escaped(client, conn, paired):
    job = ready_job(conn)
    changes = [{"question": "<script>alert(1)</script>", "what": "new on the form"}]
    past_fill(conn, job, state="sent", changes=json.dumps(changes))
    page = box(client, job)
    assert "<h3>Check these on the form</h3>" in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "<script>alert(1)" not in page
    assert "new on the form" in page


def test_no_changes_means_no_heading(client, conn, paired):
    job = ready_job(conn)
    past_fill(conn, job, state="sent", changes="[]")
    assert "Check these on the form" not in box(client, job)


# Settings → Browser plugin


def test_recent_fills_show_the_job_the_browser_and_how(client, conn, paired, phoenix):
    job = ready_job(conn)
    past_fill(conn, job, toolbar=1, browser="Laptop Chrome", state="sent")
    past_fill(conn, job, "2026-11-05T22:00:00+00:00", state="waiting")
    part = recent_part(client.get(SETTINGS).text)
    assert f'<a href="/jobs/{job}">ML Intern, Acme</a>' in part
    assert "5 Nov 14:02" in part and "5 Nov 15:00" in part
    assert "Laptop Chrome" in part and "Toolbar" in part and "your answers went to the browser" in part
    assert "No browser yet" in part and "Open and fill" in part
    assert "If you see a fill you didn't start, remove that browser." in part
    assert part.index("No browser yet") < part.index("Laptop Chrome")  # newest first


def test_recent_fills_stay_after_remove(client, door, conn, paired):
    ready_job(conn)
    assert call(door, "/plugin/fill").status_code == 200
    client.post(REMOVE, data={"id": str(paired), "csrf_token": csrf(client)})
    assert one(conn, "SELECT count(*) FROM paired_browsers")[0] == 0
    part = recent_part(client.get(SETTINGS).text)
    assert "Laptop Chrome" in part and "Toolbar" in part and "ended when its browser was removed" in part


def test_at_most_20_recent_fills_show(client, conn, paired):
    job = ready_job(conn)
    for _ in range(25):
        past_fill(conn, job)
    assert recent_part(client.get(SETTINGS).text).count(f'<a href="/jobs/{job}">') == 20


def test_no_fills_means_no_table(client, conn, paired):
    assert "Recent fills" not in client.get(SETTINGS).text


def test_a_browser_s_name_and_a_job_s_title_are_escaped(client, conn, paired):
    with conn:
        job = add_job(conn, "<script>alert(2)</script>", apply_url=LINK)
    past_fill(conn, job, browser="<script>alert(1)</script>")
    part = recent_part(client.get(SETTINGS).text)
    assert "<script>" not in part and "&lt;script&gt;alert(1)" in part and "&lt;script&gt;alert(2)" in part


def test_no_inside_word_reaches_a_page(client, conn, paired):
    job = ready_job(conn)
    changes = [{"question": "Why Acme?", "what": "its choices changed"}]
    for state, stop in WORDS:
        past_fill(conn, job, state=state, stop=stop, changes=json.dumps(changes))
    pages = [box(client, job), recent_part(client.get(SETTINGS).text), press(client, job).text]
    for page in pages:
        lowered = page.lower()
        assert "ticket" not in lowered and "pack" not in lowered and "compare" not in lowered


def test_the_rows_read_back_in_plain_words(conn):
    with conn:
        job = add_job(conn, "ML Intern", apply_url=LINK)
    assert fills.latest(conn, job) is None and fills.recent(conn) == []
    past_fill(conn, job, toolbar=1, browser="Laptop Chrome", state="sent", changes='[{"question": "Q", "what": "W"}]')
    latest = fills.latest(conn, job)
    assert latest["what"] == "your answers went to the browser"
    assert latest["changes"] == [{"question": "Q", "what": "W"}]
    assert (latest["toolbar"], latest["browser"]) == (1, "Laptop Chrome")
    (recent,) = fills.recent(conn)
    assert (recent["job"], recent["title"], recent["company"]) == (job, "ML Intern", "Acme")
