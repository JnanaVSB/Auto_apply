# ruff: noqa: F811 (door, limits, owner, paired and phone_dashboard are fixtures from test_web_plugin.py)
"""Ashby's "Read the form": the plugin reads a drawn form and the app saves it, filling nothing. Made-up jobs and forms;
nothing here opens a site."""

import html
import json
from datetime import UTC, datetime

import pytest
from helpers import add_job, box, csrf, log_in, one
from test_web_phone import ORIGIN, phone_login
from test_web_plugin import (
    FILL,
    FORM,
    LETTER_PDF,
    LINK,
    OTHER_PLUGIN,
    PLUGIN,
    SECOND_KEY,
    START,
    call,
    door,  # noqa: F401
    fetch,
    fill_rows,
    held,
    limits,  # noqa: F401
    log_out,
    ok,
    owner,  # noqa: F401
    pair,
    paired,  # noqa: F401
    paper,
    phone_dashboard,  # noqa: F401
    press,
    refused,
    report,
    send,
    sent,
)

from app import questionnaire
from app.config import Config
from app.submit import MAX_FIELDS, Field, Form, ashby, fills, match, preview, record, wordings
from app.submit import load as load_form
from app.submit import save as save_form
from app.web import actions, queries
from app.web import submit as form_part

ASHBY_LINK = "https://jobs.ashbyhq.com/acme/0a1b-2c3d"
ASHBY_FORM = f"{ASHBY_LINK}/application"
NAME = {"key": "_systemfield_name", "label": "Name", "kind": "text", "required": True}
EMAIL = {"key": "_systemfield_email", "label": "Email", "kind": "text", "required": True}
RESUME = {"key": "_systemfield_resume", "label": "Resume", "kind": "file", "required": True}
WHY = {"key": "a1b2", "label": "Why this team?", "kind": "text", "required": True}
TOP_FILE = {"key": "top-file", "label": "", "kind": "file", "required": False}
READING, NOT_READ = form_part.READING.replace("'", "&#39;"), form_part.NOT_READ
SHOWS = "Reading shows the form to the plugin. Nothing of yours goes to the site."
NOT_SEEN = (
    "The plugin can't read yes/no buttons or questions that appear after an answer: do those yourself on the form. "
    "These questions are what the page showed, and a script on the page can change them: read them before you fill."
)
SHOWN_NOT_SEEN = NOT_SEEN.replace("'", "&#39;")
READ_STOP = {"go": False, "reason": "read"}
UNEXPECTED = {"go": False, "reason": "unexpected-page"}
TOO_FEW = {"go": False, "reason": "too-few"}
STATUS = '<p id="fill-status"'
LOOKS_SENT = "It looks sent. If the site took it, press I submitted it."


@pytest.fixture
def person(conn):
    questionnaire.save(conn, questionnaire.check({
        "contact": {"full_name": "Sam Lee Park", "email": "sam@example.com"},
        "work": {"sponsorship_now": "no", "sponsorship_future": "no"},
    }))  # fmt: skip


def ashby_job(conn, data_dir, resume=True, link=ASHBY_LINK):
    with conn:
        job = add_job(conn, "ML Intern", apply_url=link)
    if resume:
        paper(conn, data_dir, job)
    return job


def read_press(client, job):
    return client.post(f"/jobs/{job}/form/read-in-browser", headers={"X-CSRF-Token": csrf(client)})


def read_ticket(owner, door, job):
    """A read ticket a browser has taken; what /plugin/start said."""
    assert read_press(owner, job).status_code == 200
    return call(door, START, ASHBY_FORM).json()


def read(owner, door, job, fields):
    return send(door, read_ticket(owner, door, job)["ticket"], fields)


def saved_keys(conn, job):
    found = load_form(conn, job)
    return found and [field.key for field in found[0].fields]


def lines(conn, data_dir, job):
    return {line.field.key: line for line in preview.preview(conn, data_dir, job).lines}


# The site's own part


def test_ashby_s_own_boxes_are_standard_and_the_rest_are_the_company_s():
    drawn = (Field("_systemfield_name", "Name", "text", True, "drawn"), Field("a1", "Why?", "text", True, "drawn"))
    assert [field.part for field in ashby.form(drawn).fields] == ["standard", "company"]


@pytest.mark.parametrize("label", ["", "   "])
def test_a_file_box_with_no_label_is_dropped(label):
    drawn = (Field("top", label, "file", False, "drawn"), Field("r", "Resume", "file", True, "drawn"))
    assert [field.key for field in ashby.form(drawn).fields] == ["r"]


def test_a_text_box_with_no_label_stays():
    assert len(ashby.form((Field("a1", "", "text", False, "drawn"),)).fields) == 1


def test_the_two_system_boxes_answer_as_name_and_email():
    assert wordings.KEYS["_systemfield_name"] == wordings.KEYS["name"]
    assert wordings.KEYS["_systemfield_email"] == wordings.KEYS["email"]


# When it can be pressed


def test_only_ashby_gets_a_read(owner, door, conn, data_dir, paired):
    for link in (LINK, "https://jobs.lever.co/acme/0a1b-2c3d"):
        job = ashby_job(conn, data_dir, link=link)
        assert fills.why_not_read(conn, queries.job(conn, job)) == ""
        assert NOT_READ in read_press(owner, job).text and not held(door)


def test_with_no_finished_resume_there_is_no_read(owner, door, conn, data_dir, paired):
    job = ashby_job(conn, data_dir, resume=False)
    assert fills.why_not_read(conn, queries.job(conn, job)) == ""
    page = box(owner, job)
    assert form_part.NO_RESUME in page and "read-in-browser" not in page and form_part.ASHBY not in page
    assert NOT_READ in read_press(owner, job).text and not held(door)


def test_with_no_browser_the_box_says_connect(owner, door, conn, data_dir):
    job = ashby_job(conn, data_dir)
    assert fills.why_not_read(conn, queries.job(conn, job)) == fills.CONNECT_FIRST
    assert html.escape(fills.CONNECT_FIRST, quote=False) in box(owner, job) and "read-in-browser" not in box(owner, job)
    assert html.escape(fills.CONNECT_FIRST, quote=False) in read_press(owner, job).text and not held(door)


def test_a_job_with_a_record_gets_no_read(owner, door, conn, data_dir, paired):
    job = ashby_job(conn, data_dir)
    with conn:
        conn.execute(
            "INSERT INTO submissions (job_id, submitted_on, saved_on, how, address, fields, files) VALUES "
            "(?, '2026-11-05', 'then', 'by hand', 'x', '[]', '[]')",
            (job,),
        )
    assert fills.why_not_read(conn, queries.job(conn, job)) == ""
    assert "read-in-browser" not in box(owner, job) and not held(door)


def test_a_job_that_must_be_opened_by_hand_gets_no_read(conn, data_dir, paired):
    job = ashby_job(conn, data_dir)
    with conn:
        conn.execute("UPDATE jobs SET open_manually_only = 1 WHERE id = ?", (job,))
    assert fills.why_not_read(conn, queries.job(conn, job)) == ""


def test_the_box_offers_it_as_open_and_fill_is_drawn(owner, conn, data_dir, paired):
    job = ashby_job(conn, data_dir)
    page = box(owner, job)
    assert form_part.ASHBY == "Ashby forms are read in the browser: press Read the form."
    assert form_part.ASHBY in page and SHOWS in page
    assert (
        f'<span hx-post="/jobs/{job}/form/read-in-browser" hx-swap="none"><a href="{ASHBY_FORM}" target="_blank" '
        'rel="noopener">Read the form</a></span>'
    ) in page
    assert page.count(STATUS) == 1


def test_the_press_says_what_comes_next(owner, conn, data_dir, paired):
    job = ashby_job(conn, data_dir)
    assert READING in read_press(owner, job).text


def test_a_busy_database_at_a_press_over_a_fill_says_so(owner, door, conn, data_dir, paired, person, limits):
    job = filled_after_a_read(owner, door, conn, data_dir)
    conn.execute("BEGIN IMMEDIATE")
    page = read_press(owner, job).text
    conn.rollback()
    assert actions.BUSY in page and NOT_READ in page and held(door)[job].mode == "fill"


# The read itself


def test_start_says_read_and_a_read_saves_from_the_browser_with_no_pack(owner, door, conn, data_dir, paired):
    job = ashby_job(conn, data_dir)
    started = read_ticket(owner, door, job)
    assert (started["mode"], started["board"]) == ("read", "ashby")
    answer = send(door, started["ticket"], [NAME, EMAIL, RESUME, WHY])
    assert answer.json() == READ_STOP
    form, _, read_from, _ = load_form(conn, job)
    assert read_from == "browser" and [field.key for field in form.fields] == [
        box_["key"] for box_ in (NAME, EMAIL, RESUME, WHY)
    ]
    assert [field.part for field in form.fields] == ["standard", "standard", "standard", "company"]


def test_no_pack_whatever_the_answers(owner, door, conn, data_dir, paired, person):
    job = ashby_job(conn, data_dir)
    assert read(owner, door, job, [NAME, EMAIL]).json() == READ_STOP
    assert "pack" not in read(owner, door, job, [NAME, EMAIL]).json()


def test_a_read_is_no_fill_past_the_cap_and_inside_the_pause(owner, door, conn, data_dir, paired, limits):
    limits(cap=1, pause=60)
    job = ashby_job(conn, data_dir)
    moment = datetime.now(UTC).isoformat(timespec="seconds")
    with conn:
        conn.execute(
            "INSERT INTO fills (job_id, address, started_on, toolbar, state) VALUES (?, ?, ?, 0, 'ended')",
            (job, ASHBY_FORM, moment),
        )
    before = fill_rows(conn)
    assert READING in read_press(owner, job).text
    assert send(door, call(door, START, ASHBY_FORM).json()["ticket"], [NAME]).json() == READ_STOP
    assert fill_rows(conn) == before


def test_a_ticket_reads_once(owner, door, conn, data_dir, paired):
    job = ashby_job(conn, data_dir)
    ticket = read_ticket(owner, door, job)["ticket"]
    assert send(door, ticket, [NAME]).json() == READ_STOP
    assert refused(send(door, ticket, [NAME, EMAIL]), 404, "no-ticket")
    assert saved_keys(conn, job) == ["_systemfield_name"] and not held(door)


def test_no_live_login_saves_nothing(owner, door, conn, data_dir, paired):
    job = ashby_job(conn, data_dir)
    ticket = read_ticket(owner, door, job)["ticket"]
    log_out(owner)
    assert refused(send(door, ticket, [NAME]), 403, "not-logged-in")
    assert load_form(conn, job) is None and held(door)
    log_in(owner)
    assert send(door, ticket, [NAME]).json() == READ_STOP


def test_another_browser_can_t_use_a_taken_ticket(owner, door, conn, data_dir, paired):
    pair(conn, SECOND_KEY, "Desk", OTHER_PLUGIN)
    job = ashby_job(conn, data_dir)
    ticket = read_ticket(owner, door, job)["ticket"]
    assert refused(send(door, ticket, [NAME], SECOND_KEY, OTHER_PLUGIN), 404, "no-ticket")
    assert load_form(conn, job) is None and held(door)


@pytest.mark.parametrize(
    ("fields", "stop"), [([], TOO_FEW), ([TOP_FILE], TOO_FEW), ([NAME] * (MAX_FIELDS + 1), UNEXPECTED)]
)
def test_nothing_to_save_is_a_stop_and_uses_the_ticket_up(owner, door, conn, data_dir, paired, fields, stop):
    job = ashby_job(conn, data_dir)
    assert read(owner, door, job, fields).json() == stop
    assert load_form(conn, job) is None and not held(door) and fill_rows(conn) == []


@pytest.mark.parametrize(
    "field", [1, None, {"key": 1}, NAME | {"kind": "made-up"}, NAME | {"required": "yes"}, NAME | {"key": "x" * 1001}]
)
def test_a_bad_field_saves_nothing_and_keeps_the_ticket(owner, door, conn, data_dir, paired, field):
    job = ashby_job(conn, data_dir)
    ticket = read_ticket(owner, door, job)["ticket"]
    assert refused(send(door, ticket, [NAME, field]), 400, "bad-request")
    assert load_form(conn, job) is None and held(door)
    assert send(door, ticket, [NAME]).json() == READ_STOP


def test_fields_that_aren_t_a_list_are_a_bad_request(owner, door, conn, data_dir, paired):
    job = ashby_job(conn, data_dir)
    ticket = read_ticket(owner, door, job)["ticket"]
    body = {"protocol": 1, "ticket": ticket, "fields": {"a": 1}}
    assert refused(door.post(FORM, json=body, headers=sent()), 400, "bad-request") and load_form(conn, job) is None


def test_a_read_ticket_gets_no_pdf(owner, door, conn, data_dir, paired):
    job = ashby_job(conn, data_dir)
    assert refused(fetch(door, read_ticket(owner, door, job)["ticket"]), 404, "no-ticket")


def test_a_busy_database_at_the_save_keeps_the_ticket(owner, door, conn, data_dir, paired):
    job = ashby_job(conn, data_dir)
    ticket = read_ticket(owner, door, job)["ticket"]
    conn.execute("BEGIN IMMEDIATE")
    response = send(door, ticket, [NAME])
    conn.rollback()
    assert refused(response, 503, "app-error") and held(door) and load_form(conn, job) is None
    assert send(door, ticket, [NAME]).json() == READ_STOP


@pytest.mark.parametrize(("state", "kind"), [("stopped", "limit"), ("stopped", "bot-check"), ("gone", None)])
def test_a_stop_on_a_read_ends_it_cleanly(owner, door, conn, data_dir, paired, state, kind):
    job = ashby_job(conn, data_dir)
    ticket = read_ticket(owner, door, job)["ticket"]
    assert ok(report(door, ticket, state, **({"kind": kind} if kind else {})))
    assert not held(door) and fill_rows(conn) == [] and load_form(conn, job) is None
    assert refused(send(door, ticket, [NAME]), 404, "no-ticket")


@pytest.mark.parametrize("state", ["filled", "reread", "clicked", "thanked"])
def test_a_read_has_no_answers_to_report(owner, door, conn, data_dir, paired, state):
    job = ashby_job(conn, data_dir)
    assert refused(report(door, read_ticket(owner, door, job)["ticket"], state), 400, "bad-request")
    assert held(door) and fill_rows(conn) == []


def test_an_untaken_read_ticket_runs_out(owner, door, conn, data_dir, paired, clock):
    job = ashby_job(conn, data_dir)
    read_press(owner, job)
    clock.now += fills.UNUSED_SECONDS + 1
    assert refused(call(door, START, ASHBY_FORM), 404, "no-ticket")


def test_the_toolbar_still_can_t_fill_an_unread_form(owner, door, conn, data_dir, paired, limits):
    job = ashby_job(conn, data_dir)
    answer = call(door, FILL, ASHBY_FORM)
    assert (answer.status_code, answer.json()) == (409, {"error": "not-ready", "job": f"/jobs/{job}"})


# What a read gives


def test_name_and_email_fill_from_the_questionnaire_and_the_resume_by_its_label(
    owner, door, conn, data_dir, paired, person
):
    job = ashby_job(conn, data_dir)
    read(owner, door, job, [NAME, EMAIL, RESUME, TOP_FILE])
    found = lines(conn, data_dir, job)
    assert found["_systemfield_name"].value == "Sam Lee Park" and found["_systemfield_email"].value == "sam@example.com"
    assert found["_systemfield_resume"].status == "fill" and found["_systemfield_resume"].source == "Resume v1"
    assert "top-file" not in found


def test_a_required_question_reaches_more_answers(owner, door, conn, data_dir, paired, person):
    job = ashby_job(conn, data_dir)
    read(owner, door, job, [NAME, WHY])
    assert WHY["label"] in repr(questionnaire.load(conn)[0])


def test_hostile_words_go_through_the_field_s_limits_and_are_escaped(owner, door, conn, data_dir, paired):
    job = ashby_job(conn, data_dir)
    hostile = WHY | {"label": "<script>alert(1)</script>\x00" + "x" * 5_000}
    read(owner, door, job, [hostile])
    label = load_form(conn, job)[0].fields[0].label
    assert "\x00" not in label and len(label) < 5_000
    page = box(owner, job)
    assert "<script>alert(1)" not in page and "&lt;script&gt;alert(1)" in page


def test_saved_answers_survive_reading_again(owner, door, conn, data_dir, paired):
    job = ashby_job(conn, data_dir)
    read(owner, door, job, [WHY])
    preview.keep(conn, job, load_form(conn, job)[0].fields[0], "Because of the robots")
    read(owner, door, job, [WHY, NAME])
    assert lines(conn, data_dir, job)["a1b2"].value == "Because of the robots"


def test_the_box_says_what_a_read_can_t_see_and_offers_read_again(owner, door, conn, data_dir, paired):
    job = ashby_job(conn, data_dir)
    assert SHOWN_NOT_SEEN not in box(owner, job)
    read(owner, door, job, [WHY])
    page = box(owner, job)
    assert "from the browser" in page and SHOWN_NOT_SEEN in page
    assert ">Read again</a></span>" in page and ">Read the form</a>" not in page and page.count(STATUS) == 1


def filled_after_a_read(owner, door, conn, data_dir):
    """A read Ashby form with every line answered, then a fill of it that a browser has taken; its job."""
    job = ashby_job(conn, data_dir)
    read(owner, door, job, [NAME, EMAIL, WHY])
    preview.keep(conn, job, load_form(conn, job)[0].fields[2], "Because of the robots")
    press(door, conn, data_dir, job)
    assert call(door, START, ASHBY_FORM).json()["mode"] == "fill"
    return job


def test_after_a_read_open_and_fill_shows_and_a_fill_compares_clean(
    owner, door, conn, data_dir, paired, person, limits
):
    job = ashby_job(conn, data_dir)
    read(owner, door, job, [NAME, EMAIL, WHY])
    assert ">Open and fill</a>" not in box(owner, job)  # a question is still open
    preview.keep(conn, job, load_form(conn, job)[0].fields[2], "Because of the robots")
    page = box(owner, job)
    assert ">Open and fill</a>" in page and page.count(STATUS) == 1
    press(door, conn, data_dir, job)
    answer = send(door, call(door, START, ASHBY_FORM).json()["ticket"], [NAME, EMAIL, WHY])
    assert [line["key"] for line in answer.json()["pack"]] == ["_systemfield_name", "_systemfield_email", "a1b2"]
    assert one(conn, "SELECT changes FROM fills")[0] == "[]"


def test_a_read_press_over_a_running_fill_ends_it_as_replaced(owner, door, conn, data_dir, paired, person, limits):
    job = filled_after_a_read(owner, door, conn, data_dir)
    assert READING in read_press(owner, job).text
    assert [row[2:] for row in fill_rows(conn)] == [("ended", "replaced")] and held(door)[job].mode == "read"


def test_a_read_press_wipes_a_read_of_the_form_kept_from_an_earlier_fill(
    owner, door, conn, data_dir, paired, person, limits
):
    job = filled_after_a_read(owner, door, conn, data_dir)
    assert ok(report(door, held(door)[job].id, "gone"))  # a read kept at gone has no ticket for ENDED to wipe
    with conn:
        conn.execute("UPDATE fills SET values_read = '{}', checked_on = 'then'")
    read_press(owner, job)
    assert one(conn, "SELECT count(values_read), count(checked_on) FROM fills") == (0, 0)


# The phone


def phone_job(phone_dashboard, conn, data_dir):
    pair(conn, plugin=PLUGIN)
    job = ashby_job(conn, data_dir)
    _, phone = phone_dashboard
    cookie, session = phone_login(phone)
    return job, phone, cookie | ORIGIN | {"X-CSRF-Token": session.csrf}


def test_the_phone_shows_no_button_and_says_why(phone_dashboard, conn, data_dir):
    job, phone, headers = phone_job(phone_dashboard, conn, data_dir)
    page = phone.get(f"/jobs/{job}/form", headers=headers).text
    assert form_part.ON_PHONE in page and "read-in-browser" not in page


def test_a_press_from_the_phone_makes_no_ticket(phone_dashboard, conn, data_dir):
    job, phone, headers = phone_job(phone_dashboard, conn, data_dir)
    answer = phone.post(f"/jobs/{job}/form/read-in-browser", headers=headers)
    assert form_part.ON_PHONE in answer.text and NOT_READ not in answer.text and not phone.app.state.tickets.tickets


def test_the_phone_says_it_once(phone_dashboard, conn, data_dir):
    job, phone, headers = phone_job(phone_dashboard, conn, data_dir)
    fields = (Field("a", "Question", "text", False, "company"),)
    save_form(conn, job, Form(fields), "browser")
    assert phone.get(f"/jobs/{job}/form", headers=headers).text.count(form_part.ON_PHONE) == 1


# A form the page wrote: nothing goes out by itself


INVENTED = [
    {"key": "phone", "label": "Favourite number", "kind": "text", "required": False},
    {"key": "x1", "label": "Salary expectations", "kind": "text", "required": False},
    {"key": "x2", "label": "GPA", "kind": "text", "required": False},
    {"key": "x3", "label": "Are you authorized to work in the US", "kind": "text", "required": False},
    {"key": "location", "label": "Tell me", "kind": "text", "required": False},
]
LETTER = {"key": "made-up-file", "label": "Cover letter", "kind": "file", "required": False}


@pytest.fixture
def full_answers(conn):
    questionnaire.save(conn, questionnaire.check({
        "contact": {"full_name": "Sam Lee Park", "email": "sam@example.com", "phone": "+1 555 0100", "city": "Tempe"},
        "work": {"sponsorship_now": "no", "sponsorship_future": "no", "authorized_us": "yes"},
        "salary": {"minimum": 90000, "currency": "USD", "per": "year"},
        "education": {"gpa": "3.9", "highest_degree": "MS"},
    }))  # fmt: skip


def why_not(conn, data_dir, job):
    limits_ = Config(data_dir=data_dir, fill_pause_minutes=0)
    return fills.why_not(conn, queries.job(conn, job), preview.preview(conn, data_dir, job), limits_)


def test_what_a_page_invents_gets_no_answer_without_your_decision(
    owner, door, conn, data_dir, paired, full_answers, limits
):
    job = ashby_job(conn, data_dir)
    read(owner, door, job, [NAME, EMAIL, *INVENTED])
    found = lines(conn, data_dir, job)
    assert not [box["key"] for box in INVENTED if found[box["key"]].status == "fill"]
    assert preview.preview(conn, data_dir, job).open >= 1
    assert why_not(conn, data_dir, job) == fills.ANSWER_FIRST and ">Open and fill</a>" not in box(owner, job)


def test_a_key_of_a_known_box_under_other_words_is_a_suggestion(owner, door, conn, data_dir, paired, full_answers):
    job = ashby_job(conn, data_dir)
    read(owner, door, job, [NAME, INVENTED[0]])
    line = lines(conn, data_dir, job)["phone"]
    assert (line.status, line.value) == ("suggested", "+1 555 0100")


def test_use_all_lets_them_go(owner, door, conn, data_dir, paired, full_answers, limits):
    job = ashby_job(conn, data_dir)
    read(owner, door, job, [NAME, EMAIL, INVENTED[0]])
    seen = form_part._seen(form_part._usable(preview.preview(conn, data_dir, job)))
    owner.post(f"/jobs/{job}/form/use-all", data={"seen": seen, "csrf_token": csrf(owner)})
    assert lines(conn, data_dir, job)["phone"].status == "fill" and why_not(conn, data_dir, job) is None
    press(door, conn, data_dir, job)
    pack = send(door, call(door, START, ASHBY_FORM).json()["ticket"], [NAME, EMAIL, INVENTED[0]]).json()["pack"]
    assert [line["key"] for line in pack] == ["_systemfield_name", "_systemfield_email", "phone"]


def letter_job(owner, door, conn, data_dir):
    """A read form with a cover letter box, and a finished letter."""
    job = ashby_job(conn, data_dir)
    paper(conn, data_dir, job, "cover_letter", body=LETTER_PDF)
    read(owner, door, job, [NAME, EMAIL, RESUME, LETTER])
    return job


def use(owner, conn, data_dir, job, key):
    line = lines(conn, data_dir, job)[key]
    data = {"field": key, "seen": form_part._seen([line]), "csrf_token": csrf(owner)}
    return owner.post(f"/jobs/{job}/form/use", data=data)


def test_a_file_box_the_page_wrote_waits_for_your_use(owner, door, conn, data_dir, paired, person):
    job = letter_job(owner, door, conn, data_dir)
    line = lines(conn, data_dir, job)["made-up-file"]
    assert (line.status, line.source) == ("suggested", "Cover letter v1")
    assert why_not(conn, data_dir, job) == fills.ANSWER_FIRST
    page = box(owner, job)
    assert "Cover letter v1" in page and "Answer it another way" not in page


def test_a_use_on_a_file_keeps_its_label_and_lets_the_pdf_go_once(owner, door, conn, data_dir, paired, person, limits):
    job = letter_job(owner, door, conn, data_dir)
    use(owner, conn, data_dir, job, "made-up-file")
    assert one(conn, "SELECT answer FROM form_answers WHERE field = 'made-up-file'") == ("Cover letter v1",)
    line = lines(conn, data_dir, job)["made-up-file"]
    assert (line.status, line.source, line.file_label, line.note) == ("fill", match.MINE, "Cover letter v1", "")
    assert record.shown(line) == "Cover letter v1"
    assert why_not(conn, data_dir, job) is None
    press(door, conn, data_dir, job)
    ticket = call(door, START, ASHBY_FORM).json()["ticket"]
    pack = send(door, ticket, [NAME, EMAIL, RESUME, LETTER]).json()["pack"]
    assert {"slot": "cover_letter"} in [line_["value"] for line_ in pack]
    assert fetch(door, ticket, "cover_letter").status_code == 200
    assert refused(fetch(door, ticket, "cover_letter"), 404, "no-ticket")


def test_a_newer_file_asks_you_again(owner, door, conn, data_dir, paired, person):
    job = letter_job(owner, door, conn, data_dir)
    use(owner, conn, data_dir, job, "made-up-file")
    paper(conn, data_dir, job, "cover_letter", version=2, body=LETTER_PDF)
    line = lines(conn, data_dir, job)["made-up-file"]
    assert (line.status, line.decision, line.value) == ("decide", "choose_file", "")
    assert "the file changed" in line.note and why_not(conn, data_dir, job) == fills.ANSWER_FIRST
    assert f'action="/jobs/{job}/form/forget"' in box(owner, job)
    owner.post(f"/jobs/{job}/form/forget", data={"field": "made-up-file", "csrf_token": csrf(owner)})
    line = lines(conn, data_dir, job)["made-up-file"]
    assert (line.status, line.source) == ("suggested", "Cover letter v2")


def test_a_consent_the_page_wrote_is_a_decision():
    field = Field("c", "I agree", "consent", False, "company")
    line = preview.held_back(match.Line(field, "fill", match.TICKED, "Questionnaire"))
    assert (line.status, line.decision, line.value) == ("decide", "consent", "")


def test_the_site_s_own_boxes_still_fill(owner, door, conn, data_dir, paired, person):
    job = ashby_job(conn, data_dir)
    read(owner, door, job, [NAME, EMAIL, RESUME])
    found = lines(conn, data_dir, job)
    assert [line.status for line in found.values()] == ["fill", "fill", "fill"]
    assert preview.preview(conn, data_dir, job).open == 0
    assert {key: kind for key, (kind, _) in ashby.OWN_KEYS.items()} == {
        "_systemfield_name": "text", "_systemfield_email": "text", "_systemfield_resume": "file",
    }  # fmt: skip


@pytest.mark.parametrize(
    ("key", "kind"), [("_systemfield_resume", "text"), ("_systemfield_email", "file"), ("_systemfield_name", "single")]
)
def test_a_site_s_own_key_on_another_kind_of_box_is_dropped(key, kind):
    assert (
        ashby.form((Field(key, "LinkedIn", kind, False, "drawn"), Field("a1", "Why?", "text", True, "drawn")))
        .fields[0]
        .key
        == "a1"
    )


def test_a_page_can_t_get_an_answer_out_through_the_resume_key(owner, door, conn, data_dir, paired, full_answers):
    job = ashby_job(conn, data_dir)
    read(
        owner,
        door,
        job,
        [NAME, EMAIL, {"key": "_systemfield_resume", "label": "LinkedIn", "kind": "text", "required": True}],
    )
    assert saved_keys(conn, job) == ["_systemfield_name", "_systemfield_email"]


def test_an_own_key_of_another_kind_is_held_back_even_if_it_was_saved():
    field = Field("_systemfield_resume", "LinkedIn", "text", False, "company")
    assert preview.held_back(match.Line(field, "fill", "made-up", "Questionnaire")).status == "suggested"


SYMBOL = {"key": "age", "label": "Are you 18?", "kind": "text", "required": True}


def test_a_symbol_change_holds_your_answer_back_and_keeps_the_row(owner, door, conn, data_dir, paired):
    job = ashby_job(conn, data_dir)
    read(owner, door, job, [NAME, SYMBOL])
    preview.keep(conn, job, load_form(conn, job)[0].fields[1], "Yes")
    assert lines(conn, data_dir, job)["age"].source == match.MINE
    read(owner, door, job, [NAME, SYMBOL | {"label": "Are you < 18?"}])
    line = lines(conn, data_dir, job)["age"]
    assert line.source != match.MINE and "the question changed" in line.note
    assert one(conn, "SELECT answer, wording FROM form_answers WHERE field = 'age'") == ("Yes", "")


def test_a_symbol_change_in_a_choice_counts_too(owner, door, conn, data_dir, paired):
    job = ashby_job(conn, data_dir)
    choice = {"key": "pick", "label": "Pick", "kind": "single", "required": True}
    read(owner, door, job, [NAME, choice | {"choices": [{"label": "A+", "value": "1"}]}])
    preview.keep(conn, job, load_form(conn, job)[0].fields[1], "A+")
    read(owner, door, job, [NAME, choice | {"choices": [{"label": "A-", "value": "1"}]}])
    assert one(conn, "SELECT wording FROM form_answers WHERE field = 'pick'") == ("",)


def test_the_same_words_keep_your_answer(owner, door, conn, data_dir, paired):
    job = ashby_job(conn, data_dir)
    read(owner, door, job, [NAME, SYMBOL])
    preview.keep(conn, job, load_form(conn, job)[0].fields[1], "Yes")
    read(owner, door, job, [NAME, SYMBOL | {"label": "  ARE you   18?"}])
    assert lines(conn, data_dir, job)["age"].source == match.MINE


def test_a_busy_database_changes_neither_the_form_nor_your_answer(owner, door, conn, data_dir, paired):
    job = ashby_job(conn, data_dir)
    read(owner, door, job, [NAME, SYMBOL])
    preview.keep(conn, job, load_form(conn, job)[0].fields[1], "Yes")
    ticket = read_ticket(owner, door, job)["ticket"]
    conn.execute("BEGIN IMMEDIATE")
    response = send(door, ticket, [NAME, SYMBOL | {"label": "Are you < 18?"}])
    conn.rollback()
    assert refused(response, 503, "app-error") and held(door)
    assert (
        load_form(conn, job)[0].fields[1].label == "Are you 18?"
        and lines(conn, data_dir, job)["age"].source == match.MINE
    )


@pytest.mark.parametrize(
    ("key", "label"),
    [
        ("_systemfield_name", "Type your full name to sign this binding arbitration agreement and waive your rights"),
        ("_systemfield_email", "I agree you may sell this address; I certify I used no AI"),
        ("_systemfield_name", "Name of your employer"),
    ],
)  # fmt: skip
def test_a_site_s_own_box_under_other_words_waits_for_you(
    owner, door, conn, data_dir, paired, person, limits, key, label
):
    job = ashby_job(conn, data_dir)
    read(
        owner,
        door,
        job,
        [EMAIL if key != EMAIL["key"] else NAME, {"key": key, "label": label, "kind": "text", "required": True}],
    )
    assert lines(conn, data_dir, job)[key].status != "fill"
    assert why_not(conn, data_dir, job) == fills.ANSWER_FIRST


def test_the_resume_key_labelled_as_a_letter_waits_for_your_use(owner, door, conn, data_dir, paired, person):
    job = ashby_job(conn, data_dir)
    paper(conn, data_dir, job, "cover_letter", body=LETTER_PDF)
    read(owner, door, job, [NAME, RESUME | {"label": "Cover letter"}])
    assert lines(conn, data_dir, job)["_systemfield_resume"].status == "suggested"


@pytest.mark.parametrize(
    ("key", "kind", "label"),
    [
        ("_systemfield_name", "text", "Name*"), ("_systemfield_name", "text", "Full Name"),
        ("_systemfield_name", "text", "Legal name"), ("_systemfield_name", "text", "Your name"),
        ("_systemfield_email", "text", "Email"), ("_systemfield_email", "text", "E-mail address"),
        ("_systemfield_email", "text", "Your email"), ("_systemfield_resume", "file", "Resume"),
        ("_systemfield_resume", "file", "Resume/CV"), ("_systemfield_resume", "file", "CV"),
    ],
)  # fmt: skip
def test_a_site_s_own_box_in_its_plain_words_is_its_own(key, kind, label):
    assert ashby.own_box(Field(key, label, kind, True, "standard"))


def test_a_question_dropped_and_brought_back_changed_is_your_decision(owner, door, conn, data_dir, paired):
    job = ashby_job(conn, data_dir)
    read(owner, door, job, [NAME, EMAIL, SYMBOL])
    preview.keep(conn, job, load_form(conn, job)[0].fields[2], "Yes")
    read(owner, door, job, [NAME, EMAIL])
    read(owner, door, job, [NAME, EMAIL, SYMBOL | {"label": "Are you < 18?"}])
    line = lines(conn, data_dir, job)["age"]
    assert line.source != match.MINE and "the question changed" in line.note


def test_a_question_dropped_and_brought_back_the_same_asks_again(owner, door, conn, data_dir, paired):
    job = ashby_job(conn, data_dir)
    read(owner, door, job, [NAME, EMAIL, SYMBOL])
    preview.keep(conn, job, load_form(conn, job)[0].fields[2], "Yes")
    read(owner, door, job, [NAME, EMAIL])
    read(owner, door, job, [NAME, EMAIL, SYMBOL])
    assert lines(conn, data_dir, job)["age"].source != match.MINE
    assert one(conn, "SELECT answer FROM form_answers WHERE field = 'age'") == ("Yes",)  # the row stays


def test_one_read_can_t_flood_more_answers_and_says_so(owner, door, conn, data_dir, paired, person):
    job = ashby_job(conn, data_dir)
    many = [
        {"key": f"j{number}", "label": f"Made-up question {number}", "kind": "text", "required": True}
        for number in range(40)
    ]
    assert read(owner, door, job, many).json() == READ_STOP
    assert len(questionnaire.load(conn)[0]["more"]) == form_part.MAX_REMEMBERED
    words = "Only the first 25 of this form's questions were kept in More answers: answer the rest here."
    assert load_form(conn, job)[3] == words
    assert words.replace("'", "&#39;") in box(owner, job)


def test_that_note_sits_beside_a_could_not_be_read_line(owner, door, conn, data_dir, paired, person):
    job = ashby_job(conn, data_dir)
    many = [
        {"key": f"j{number}", "label": f"Made-up question {number}", "kind": "text", "required": True}
        for number in range(40)
    ]
    read(owner, door, job, many)
    read(owner, door, job, many[:38])
    problem = load_form(conn, job)[3]
    assert "2 of this form's questions could not be read" in problem and "Only the first 25" in problem


def test_a_full_list_says_so_on_the_form(owner, door, conn, data_dir, paired, person, monkeypatch):
    monkeypatch.setattr(questionnaire, "MAX_PAIRS", 1)
    questionnaire.remember(conn, "A made-up earlier question", "Acme")
    job = ashby_job(conn, data_dir)
    read(owner, door, job, [WHY])
    assert load_form(conn, job)[3] == questionnaire.FULL


def test_a_site_that_reads_itself_has_no_such_limit(conn, data_dir, person):
    with conn:
        job = add_job(conn, "ML Intern", apply_url=LINK)
    many = tuple(Field(f"j{number}", f"Made-up question {number}", "text", True, "company") for number in range(30))
    save_form(conn, job, Form(many), "api")
    assert form_part.remember(conn, data_dir, queries.job(conn, job)) is None
    assert len(questionnaire.load(conn)[0]["more"]) == 30


@pytest.mark.parametrize("label", ["Passcode", "PIN", "Your pin", "pass code word password"])
def test_a_question_about_a_secret_is_never_in_a_read(door, conn, data_dir, paired, label):
    job = ashby_job(conn, data_dir)
    known = {"key": "secret", "label": label, "kind": "text", "required": False}
    ready = ready_form(door, conn, data_dir, job, known)
    assert ok(
        report(
            door,
            ready,
            "reread",
            values=[{"key": "_systemfield_name", "value": "Fake"}, {"key": "secret", "value": "x"}],
        )
    )
    assert "secret" not in json.loads(one(conn, "SELECT values_read FROM fills")[0])


def test_pinterest_is_no_pin(door, conn, data_dir, paired):
    job = ashby_job(conn, data_dir)
    known = {"key": "pins", "label": "Pinterest", "kind": "text", "required": False}
    ready = ready_form(door, conn, data_dir, job, known)
    assert ok(report(door, ready, "reread", values=[{"key": "pins", "value": "Fake"}]))
    assert json.loads(one(conn, "SELECT values_read FROM fills")[0]) == {"pins": "Fake"}


def ready_form(door, conn, data_dir, job, extra):
    """A running fill of a form saved with `extra` beside the name, with its answers out; its ticket."""
    fields = (
        Field("_systemfield_name", "Name", "text", True, "standard"),
        Field(extra["key"], extra["label"], "text", False, "company"),
    )
    save_form(conn, job, Form(fields), "browser")
    preview.keep(conn, job, fields[0], "Fake")
    preview.keep(conn, job, fields[1], "Fake")
    press(door, conn, data_dir, job)
    ticket = call(door, START, ASHBY_FORM).json()["ticket"]
    assert send(door, ticket, [NAME | {"label": "Name"}, extra]).json()["go"]
    return ticket


def test_your_own_answer_stays_a_fill(owner, door, conn, data_dir, paired, person):
    job = ashby_job(conn, data_dir)
    read(owner, door, job, [WHY])
    preview.keep(conn, job, load_form(conn, job)[0].fields[0], "Because of the robots")
    line = lines(conn, data_dir, job)["a1b2"]
    assert (line.status, line.value) == ("fill", "Because of the robots")


def test_lines_the_writer_sees_are_not_changed(owner, door, conn, data_dir, paired, full_answers):
    job = ashby_job(conn, data_dir)
    read(owner, door, job, [NAME, INVENTED[0]])
    form = load_form(conn, job)[0]
    found = {line.field.key: line for line in preview.lines(conn, form, job)[0]}
    assert found["phone"].status == "fill"


def test_read_again_with_fewer_questions_says_how_many_went(owner, door, conn, data_dir, paired):
    job = ashby_job(conn, data_dir)
    read(owner, door, job, [NAME, EMAIL, WHY])
    assert load_form(conn, job)[3] is None  # a first read says nothing
    assert read(owner, door, job, [NAME, EMAIL]).json() == READ_STOP
    assert "1 of this form's questions could not be read" in load_form(conn, job)[3]
    assert "1 of this form&#39;s questions could not be read" in box(owner, job)


@pytest.mark.parametrize("kept", [0, 1, 3])
def test_a_read_with_under_half_the_saved_questions_keeps_the_saved_form(owner, door, conn, data_dir, paired, kept):
    job = ashby_job(conn, data_dir)
    boxes = [WHY | {"key": f"q{number}", "label": f"Question {number}"} for number in range(8)]
    read(owner, door, job, boxes)
    drawn = [*boxes[:kept], WHY | {"key": "new", "label": "A new question"}]
    assert read(owner, door, job, drawn).json() == TOO_FEW
    assert saved_keys(conn, job) == [box_["key"] for box_ in boxes] and not held(door)


def test_a_read_with_exactly_half_the_saved_questions_saves(owner, door, conn, data_dir, paired):
    job = ashby_job(conn, data_dir)
    boxes = [WHY | {"key": f"q{number}", "label": f"Question {number}"} for number in range(4)]
    read(owner, door, job, boxes)
    assert read(owner, door, job, boxes[:2]).json() == READ_STOP
    assert saved_keys(conn, job) == ["q0", "q1"]


def test_ashby_words_show_only_with_a_form_to_read(owner, conn, data_dir, paired):
    job = ashby_job(conn, data_dir)
    with conn:
        conn.execute("UPDATE jobs SET open_manually_only = 1 WHERE id = ?", (job,))
    assert form_part.ASHBY not in box(owner, job)


def test_a_read_press_drops_it_looks_sent(owner, door, conn, data_dir, paired, person, limits):
    job = filled_after_a_read(owner, door, conn, data_dir)
    ticket = held(door)[job].id
    assert ok(report(door, ticket, "gone"))
    with conn:
        conn.execute("UPDATE fills SET clicked_on = 'then', state = 'sent'")
    assert LOOKS_SENT in box(owner, job)
    read_press(owner, job)
    assert one(conn, "SELECT clicked_on FROM fills")[0] is None and LOOKS_SENT not in box(owner, job)


def test_read_again_with_the_same_or_more_questions_says_nothing(owner, door, conn, data_dir, paired):
    job = ashby_job(conn, data_dir)
    read(owner, door, job, [NAME])
    read(owner, door, job, [NAME, EMAIL])
    assert load_form(conn, job)[3] is None


def test_a_form_the_app_read_itself_isn_t_held_back(conn, data_dir, person):
    with conn:
        job = add_job(conn, "ML Intern", apply_url=LINK)
    save_form(conn, job, Form((Field("phone", "Favourite number", "text", False, "standard"),)), "api")
    questionnaire.save(conn, questionnaire.check({"contact": {"phone": "+1 555 0100"}}))
    assert lines(conn, data_dir, job)["phone"].status == "fill"


def test_the_box_says_a_page_can_change_the_questions():
    assert "a script on the page can change them: read them before you fill" in form_part.NOT_SEEN
