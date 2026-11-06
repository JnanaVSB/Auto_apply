# ruff: noqa: F811 (door, owner and paired are fixtures from test_web_plugin.py)
"""A record frozen from what the plugin last read of the form: each line, each file, the words around them and when the
read goes. Made-up jobs, forms and answers; nothing here opens a site."""

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest
import sqlcipher3
from helpers import add_job, csrf, one
from test_submit_fills import busy
from test_web_plugin import (
    FORM_ADDRESS,
    LINK,
    RESUME_PDF,
    VALUE,
    answered,
    door,  # noqa: F401
    fetch,
    held,
    ok,
    owner,  # noqa: F401
    packed,
    paired,  # noqa: F401
    paper,
    ready_job,
    refused,
    report,
)

from app.export import papers
from app.submit import Choice, Field, match, record
from app.web import queries

AT = "2026-11-06T21:05:00+00:00"
HEADING = "What the form held when last checked (6 Nov 14:05)"
BY_HAND = "What the app prepared (you applied by hand)"
CAUTION = (
    "This is what the form showed the plugin, not proof of what the site received. Only questions the app knew about "
    "are here."
)
NOT_READ = "the plugin couldn't read this box"
AS_SHOWN = "as the form showed it"
NO_FILE = "its box showed no file when last checked"
ANOTHER_FILE = "the box showed another file"
ON_BOARD = "INSERT INTO tracker VALUES (?, 'applied', NULL, '2026-10-17', 'then')"
ADD_FILL = """INSERT INTO fills (job_id, address, started_on, toolbar, state, values_read, checked_on)
    VALUES (?, ?, 'then', 0, 'sent', ?, ?)"""
HELD = "SELECT count(values_read), count(checked_on) FROM fills"
A_PERSON_S_FILE = "Fake-Person-Resume-2026.pdf"
MARKER = "fake-answer-only-in-the-read"
JOB = {"company": "Acme", "title": "ML Intern"}


@pytest.fixture(autouse=True)
def laptop_clock(phoenix):
    """Every test on a laptop in Phoenix, 7 hours behind UTC."""


def on_board(conn):
    with conn:
        job = add_job(conn, "ML Intern", apply_url=LINK)
        conn.execute(ON_BOARD, (job,))
    return job


def fill(conn, job, read=None, checked_on=AT):
    with conn:
        conn.execute(ADD_FILL, (job, FORM_ADDRESS, read and json.dumps(read), read and checked_on))


def line(key="q", status="fill", value="Fake", kind="text", source="Questionnaire", note=""):
    return match.Line(Field(key, f"Question {key}", kind, False, "company"), status, value, source, note=note)


def frozen(conn, data_dir, read, *lines):
    """The record of a job with this read held, rows only: no ticket, as after a restart."""
    job = on_board(conn)
    fill(conn, job, read)
    record.submit(conn, data_dir, queries.job(conn, job), date.today(), lines or (line(),))
    return record.recorded(conn, job)


def row(found, key="q"):
    return next(field for field in found["fields"] if field["question"] == f"Question {key}")


def seen(found, key="q"):
    return row(found, key)["value"], row(found, key)["from"]


def paper_text(found):
    title, sections = papers.filled_answers(JOB, found, None, {})
    return "\n".join([title, *(str(body) for _, body in sections)])


# Which kind of record


def test_with_no_read_it_is_by_hand_word_for_word(conn, data_dir):
    found = frozen(conn, data_dir, None)
    assert (found["how"], found["checked_on"]) == ("by hand", None)
    assert record.heading(found) == BY_HAND == "What the app prepared (you applied by hand)"
    assert (
        record.caution(found) == record.MAY_DIFFER == "The site may hold different answers if you changed them there."
    )
    assert seen(found) == ("Fake", "Questionnaire")


def test_with_a_read_it_is_the_browser_s_and_keeps_the_read_s_time(conn, data_dir):
    found = frozen(conn, data_dir, {"q": "Fake"})
    assert (found["how"], found["checked_on"]) == ("browser", AT)
    assert record.heading(found) == HEADING and record.caution(found) == CAUTION


def test_with_no_form_read_it_is_by_hand_and_the_read_still_goes(conn, data_dir):
    job = on_board(conn)
    fill(conn, job, {"q": MARKER})
    record.submit(conn, data_dir, queries.job(conn, job), date.today(), None)
    found = record.recorded(conn, job)
    assert (found["how"], found["checked_on"]) == ("by hand", None)
    assert one(conn, HELD) == (0, 0) and MARKER not in repr(found)


def test_only_the_newest_row_counts(conn, data_dir):
    job = on_board(conn)
    fill(conn, job, {"q": MARKER})
    fill(conn, job)
    record.submit(conn, data_dir, queries.job(conn, job), date.today(), (line(),))
    found = record.recorded(conn, job)
    assert (found["how"], found["checked_on"]) == ("by hand", None) and MARKER not in repr(found)
    assert one(conn, HELD) == (0, 0)  # the older row's read goes all the same


def test_another_job_s_read_counts_for_nothing_and_stays(conn, data_dir):
    other = ready_job(conn, "https://jobs.lever.co/acme/9")
    fill(conn, other, {"q": MARKER})
    assert frozen(conn, data_dir, None)["how"] == "by hand"
    assert one(conn, HELD) == (1, 1)


def test_the_save_takes_every_row_s_read_with_it(conn, data_dir):
    job = on_board(conn)
    fill(conn, job, {"q": "old"})
    fill(conn, job, {"q": "Fake"})
    record.submit(conn, data_dir, queries.job(conn, job), date.today(), (line(),))
    assert record.recorded(conn, job)["how"] == "browser" and one(conn, HELD) == (0, 0)


def test_a_card_that_moved_rolls_the_wipe_back_too(conn, data_dir, monkeypatch):
    with conn:
        job = add_job(conn, "ML Intern", apply_url=LINK)
        conn.execute("INSERT INTO tracker VALUES (?, 'ready', NULL, NULL, 'then')", (job,))
    monkeypatch.setattr(record.runs, "last_done", lambda *_: {"version": 1})
    fill(conn, job, {"q": "Fake"})
    with monkeypatch.context() as moment:
        moment.setattr(record.tracker, "moved", lambda *_: False)
        with pytest.raises(record.Refused):
            record.submit(conn, data_dir, queries.job(conn, job), date.today(), (line(),))
    assert record.recorded(conn, job) is None and one(conn, HELD) == (1, 1)


def test_a_second_press_changes_nothing(conn, data_dir):
    job = on_board(conn)
    fill(conn, job, {"q": "Fake"})
    record.submit(conn, data_dir, queries.job(conn, job), date.today(), (line(),))
    before = record.recorded(conn, job)
    fill(conn, job, {"q": MARKER})
    with pytest.raises(record.Refused):
        record.submit(conn, data_dir, queries.job(conn, job), date.today(), (line(),))
    assert record.recorded(conn, job) == before and one(conn, HELD) == (1, 1)


def test_a_busy_database_saves_nothing_and_keeps_the_read(conn, data_dir):
    job = on_board(conn)
    fill(conn, job, {"q": "Fake"})
    with busy(conn, data_dir), pytest.raises(sqlcipher3.OperationalError):
        record.submit(conn, data_dir, queries.job(conn, job), date.today(), (line(),))
    assert record.recorded(conn, job) is None and one(conn, HELD) == (1, 1)


# Each line


@pytest.mark.parametrize(
    ("read", "expected"),
    [
        ({"other": "x"}, ("", NOT_READ)),
        ({"q": ""}, ("", record.EMPTY_FROM)),
        ({"q": "  \n"}, ("", record.EMPTY_FROM)),
        ({"q": []}, ("", record.EMPTY_FROM)),
        ({"q": ["", " "]}, ("", record.EMPTY_FROM)),
        ({"q": "Fake"}, ("Fake", "Questionnaire")),
        ({"q": "  FAKE "}, ("Fake", "Questionnaire")),
        ({"q": "Something else"}, ("Something else", AS_SHOWN)),
        ({"q": ["Red", "Blue"]}, ("Red, Blue", AS_SHOWN)),
    ],
)
def test_each_line_says_what_the_read_says(conn, data_dir, read, expected):
    assert seen(frozen(conn, data_dir, read | {"z": "keeps it a read"}, line())) == expected


def test_spacing_inside_a_value_is_no_difference(conn, data_dir):
    prepared = line(value="Two  words here")
    assert seen(frozen(conn, data_dir, {"q": "two words\nHERE"}, prepared)) == ("Two  words here", "Questionnaire")


def test_a_multi_choice_read_as_prepared_is_the_same(conn, data_dir):
    field = Field("q", "Question q", "multi", False, "company", choices=(Choice("Red", "r"), Choice("Blue", "b")))
    prepared = match.Line(field, "fill", ("r", "b"), "Questionnaire")
    assert seen(frozen(conn, data_dir, {"q": ["Red", "Blue"]}, prepared)) == ("Red, Blue", "Questionnaire")


@pytest.mark.parametrize("status", ["decide", "suggested", "left", "write", "empty"])
def test_a_line_the_app_had_no_value_for_shows_what_the_form_held(conn, data_dir, status):
    open_line = line(status=status, value="")
    assert seen(frozen(conn, data_dir, {"q": "Mine"}, open_line)) == ("Mine", AS_SHOWN)


def test_a_note_stays_and_hidden_lines_stay_out(conn, data_dir):
    found = frozen(
        conn, data_dir, {"q": "Other", "h": MARKER}, line(note="split from your full name"), line("h", "hidden")
    )
    assert row(found)["note"] == "split from your full name"
    assert len(found["fields"]) == 1 and MARKER not in repr(found)


def test_a_long_value_is_cut(conn, data_dir):
    assert len(seen(frozen(conn, data_dir, {"q": ["x" * 20_000, "y" * 20_000]}))[0]) == 20_000


# Each file


def file_line(data_dir):
    return line("resume", value=str(data_dir / "documents" / "a.pdf"), kind="file", source="Resume v1")


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        ("same", ("Resume v1", "Resume v1")),
        ("changed", (ANOTHER_FILE, AS_SHOWN)),
        ("", ("", NO_FILE)),
    ],
)
def test_a_file_line_goes_by_its_verdict(conn, data_dir, verdict, expected):
    assert (
        seen(frozen(conn, data_dir, {"resume": verdict, "q": "Fake"}, file_line(data_dir), line()), "resume")
        == expected
    )


@pytest.mark.parametrize("said", ["text from when this was a text box", ["a list"], 1, None])
def test_a_file_line_with_anything_but_a_verdict_could_not_be_read(conn, data_dir, said):
    found = frozen(conn, data_dir, {"resume": said, "q": "Fake"}, file_line(data_dir), line())
    assert seen(found, "resume") == ("", NOT_READ)


def test_the_record_names_the_pdf_that_went_out(owner, door, conn, data_dir, paired):
    job, ticket, _ = packed(door, conn, data_dir)
    with conn:
        conn.execute(ON_BOARD, (job,))
    assert fetch(door, ticket).status_code == 200
    held_out = json.loads(one(conn, "SELECT pdfs FROM fills")[0])
    assert held_out == {"resume": {"version": 1, "sha256": hashlib.sha256(RESUME_PDF).hexdigest()}}
    paper(conn, data_dir, job, version=2, body=b"%PDF-1.7 a newer made-up resume")
    assert ok(file_read(door, ticket))
    submitted(owner, job)
    (entry,) = record.recorded(conn, job)["files"]
    assert (entry["label"], entry["version"]) == ("Resume v1", 1)
    assert entry["sha256"] == hashlib.sha256(RESUME_PDF).hexdigest()


def test_a_pdf_that_didn_t_go_out_is_the_newest_as_before(owner, door, conn, data_dir, paired):
    job, ticket, _ = packed(door, conn, data_dir)
    with conn:
        conn.execute(ON_BOARD, (job,))
    paper(conn, data_dir, job, version=2, body=b"%PDF-1.7 a newer made-up resume")
    assert ok(file_read(door, ticket))
    submitted(owner, job)
    (entry,) = record.recorded(conn, job)["files"]
    assert entry["label"] == "Resume v2"


def file_read(door, ticket):
    return report(door, ticket, "reread", values=[VALUE, {"key": "resume", "value": "Resume.pdf"}])


def test_a_file_line_with_no_verdict_could_not_be_read(conn, data_dir):
    found = frozen(conn, data_dir, {"q": "Fake"}, file_line(data_dir), line())
    assert seen(found, "resume") == ("", NOT_READ)


def filled_form(owner, door, conn, data_dir, name):
    """A fill with a resume in its pack, read with this name in the file box, then recorded; its job."""
    job, ticket, _ = packed(door, conn, data_dir)
    with conn:
        conn.execute(ON_BOARD, (job,))
    assert ok(report(door, ticket, "reread", values=[VALUE, {"key": "resume", "value": name}]))
    submitted(owner, job)
    return job


def submitted(client, job):
    return client.post(f"/jobs/{job}/form/submitted", data={"on": date.today().isoformat(), "csrf_token": csrf(client)})


def test_a_changed_file_is_marked_on_its_slot_and_no_name_is_kept(owner, door, conn, data_dir, paired):
    job = filled_form(owner, door, conn, data_dir, A_PERSON_S_FILE)
    found = record.recorded(conn, job)
    assert [file.get("changed") for file in found["files"] if file["slot"] == "resume"] == [True]
    page = owner.get(f"/jobs/{job}/form").text
    assert page.count(ANOTHER_FILE) == 2  # its line, and its file
    assert A_PERSON_S_FILE not in repr(found) and A_PERSON_S_FILE not in page


def test_the_same_file_is_not_marked(owner, door, conn, data_dir, paired):
    job = filled_form(owner, door, conn, data_dir, "Resume.pdf")
    assert not [file for file in record.recorded(conn, job)["files"] if "changed" in file]
    assert ANOTHER_FILE not in owner.get(f"/jobs/{job}/form").text


# The words around a record


def test_a_browser_record_never_claims_what_was_sent_or_what_you_did(owner, door, conn, data_dir, paired):
    job = ready_job(conn)
    ticket = answered(door, conn, data_dir, job)
    with conn:
        conn.execute(ON_BOARD, (job,))
    assert ok(report(door, ticket, "reread", values=[VALUE | {"value": "Written by a script"}]))
    submitted(owner, job)
    found = record.recorded(conn, job)
    assert found["how"] == "browser" and found["fields"][0]["value"] == "Written by a script"
    assert found["fields"][0]["from"] == AS_SHOWN
    for text in (owner.get(f"/jobs/{job}/form").text, paper_text(found)):
        assert "What the form held when last checked (" in text and CAUTION in text
        assert "What was sent" not in text and "you changed" not in text.lower() and BY_HAND not in text


def test_the_old_words_are_gone():
    assert "What was sent" not in Path(record.__file__).read_text("utf-8")


def test_a_script_in_a_read_is_escaped_in_the_box(owner, door, conn, data_dir, paired):
    job = ready_job(conn)
    ticket = answered(door, conn, data_dir, job)
    with conn:
        conn.execute(ON_BOARD, (job,))
    assert ok(report(door, ticket, "reread", values=[VALUE | {"value": "<script>alert(1)</script>"}]))
    submitted(owner, job)
    page = owner.get(f"/jobs/{job}/form").text
    assert "<script>alert(1)" not in page and "&lt;script&gt;alert(1)" in page


def test_the_paper_says_the_heading_the_caution_and_the_changed_file(conn, data_dir):
    found = frozen(conn, data_dir, {"q": "Fake"}) | {
        "files": [
            {"slot": "resume", "label": "Resume v1", "version": 1, "sha256": "a" * 64, "note": "", "changed": True}
        ]
    }
    text = paper_text(found)
    assert text.startswith(HEADING) and CAUTION in text and ANOTHER_FILE in text


def test_a_by_hand_paper_keeps_its_words(conn, data_dir):
    found = frozen(conn, data_dir, None)
    text = paper_text(found)
    assert text.startswith(BY_HAND) and record.MAY_DIFFER in text and CAUTION not in text and ANOTHER_FILE not in text


def test_a_file_row_with_no_changed_key_reads_as_before():
    row_ = {"slot": "resume", "label": "Resume v1", "version": 1, "sha256": "a" * 64, "note": ""}
    assert papers.file_notes(row_, None) == [] and papers.file_notes(row_ | {"changed": True}, None) == [ANOTHER_FILE]


def test_an_old_by_hand_row_still_reads_back(conn):
    with conn:
        job = add_job(conn, "ML Intern", apply_url=LINK)
        conn.execute(
            "INSERT INTO submissions (job_id, submitted_on, saved_on, how, address, fields, files) VALUES "
            "(?, '2026-10-17', 'then', 'by hand', 'x', '[]', '[]')",
            (job,),
        )
    found = record.recorded(conn, job)
    assert found["checked_on"] is None and record.heading(found) == BY_HAND


# A record never changes


def test_a_later_read_finds_no_ticket_and_changes_nothing(owner, door, conn, data_dir, paired):
    job = ready_job(conn)
    ticket = answered(door, conn, data_dir, job)
    with conn:
        conn.execute(ON_BOARD, (job,))
    assert ok(report(door, ticket, "reread", values=[VALUE]))
    submitted(owner, job)
    before = record.recorded(conn, job)
    assert before["how"] == "browser" and one(conn, HELD) == (0, 0)
    assert refused(report(door, ticket, "reread", values=[VALUE | {"value": MARKER}]), 404, "no-ticket")
    assert record.recorded(conn, job) == before and one(conn, HELD) == (0, 0)


def test_a_ticket_that_outlived_the_record_changes_nothing(owner, door, conn, data_dir, paired, monkeypatch):
    job = ready_job(conn)
    ticket = answered(door, conn, data_dir, job)
    with conn:
        conn.execute(ON_BOARD, (job,))
    assert ok(report(door, ticket, "reread", values=[VALUE]))

    def busy(*_):
        raise sqlcipher3.OperationalError("database is locked")

    with monkeypatch.context() as moment:
        moment.setattr(owner.app.state.tickets, "end_job", busy)
        assert submitted(owner, job).status_code == 503
    before = record.recorded(conn, job)
    assert before["how"] == "browser" and held(door)
    assert refused(report(door, ticket, "reread", values=[VALUE | {"value": MARKER}]), 404, "no-ticket")
    assert record.recorded(conn, job) == before and one(conn, HELD) == (0, 0) and not held(door)
