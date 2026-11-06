"""What a fill's rows say (Phase 7): the stop kinds, and each row in the plain words the job's box and Settings show.
Only reads: fills.py writes the rows."""

import json
from datetime import datetime, timedelta

import sqlcipher3

from app.collect import now

SIGN_DAYS = 7  # how far back the first page counts toolbar fills
LATEST = """SELECT started_on, toolbar, browser, state, stop_kind, changes, filled, files, taken_on, checked_on,
    clicked_on FROM fills WHERE job_id = ? ORDER BY id DESC LIMIT 1"""
TOOLBAR_FILLS = "SELECT count(*) FROM fills WHERE toolbar = 1 AND started_on >= ?"
ALL_FILLS = "SELECT count(*) FROM fills JOIN jobs ON jobs.id = fills.job_id"
RECENT = """SELECT fills.started_on, fills.toolbar, fills.browser, fills.state, fills.stop_kind, fills.changes,
    fills.filled, fills.files, fills.taken_on, fills.checked_on, fills.clicked_on, jobs.id, jobs.title, jobs.company
    FROM fills JOIN jobs ON jobs.id = fills.job_id ORDER BY fills.id DESC LIMIT ?"""
UNEXPECTED_PAGE, TOO_MANY_FIELDS, NOTHING_TO_FILL = "unexpected-page", "too-many-fields", "nothing-to-fill"
BOT_CHECK, LIMIT, CANT_SET = "bot-check", "limit", "cant-set"
BROWSER_REMOVED, REPLACED, SUBMITTED, LOGGED_OUT = "browser-removed", "replaced", "submitted", "logged-out"
GONE, NOT_SENT = "gone", "not-sent"
SAME_FILE, CHANGED_FILE = "same", "changed"  # all a read keeps of a file box
HAPPENED = {  # a row's state and stop kind, as the job's box and Settings say them
    ("waiting", None): "no browser has opened it yet",
    ("started", None): "opened in a browser",
    ("sent", None): "your answers went to the browser",
    ("sent", BROWSER_REMOVED): "your answers went to the browser, then it was removed",
    ("sent", REPLACED): "your answers went to the browser, then a newer press ended the fill",
    ("sent", SUBMITTED): "your answers went to the browser, then you pressed I submitted it",
    ("sent", GONE): "your answers went to the browser, then the form's tab closed or moved on",
    ("sent", NOT_SENT): "your answers went to the browser, then you said you didn't send it",
    ("sent", BOT_CHECK): (
        "your answers went to the browser, then the fill stopped: the site wants to check you are a person. Finish it "
        "in the open tab, then check the form."
    ),
    ("sent", LIMIT): (
        "your answers went to the browser, then the fill stopped: the site says you've reached its limit on "
        "applications. Check the open tab."
    ),
    ("sent", UNEXPECTED_PAGE): (
        "your answers went to the browser, then the fill stopped: the page changed to something else. Check the open "
        "tab and fill in the rest yourself."
    ),
    ("sent", CANT_SET): (
        "your answers went to the browser, then the fill stopped: the form wouldn't let the plugin set any box. Fill "
        "it in yourself in the open tab."
    ),
    ("filled", None): "the plugin filled what it could. Check the form in the open tab, then press Submit yourself.",
    ("filled", BROWSER_REMOVED): "the plugin filled what it could, then its browser was removed",
    ("filled", REPLACED): "the plugin filled what it could, then a newer press ended the fill",
    ("filled", SUBMITTED): "the plugin filled what it could, then you pressed I submitted it",
    ("filled", GONE): "the plugin filled what it could, then the form's tab closed or moved on",
    ("filled", NOT_SENT): "the plugin filled what it could, then you said you didn't send it",
    ("stopped", BOT_CHECK): (
        "stopped: the site wants to check you are a person. Finish it in the open tab, then fill in the rest yourself: "
        "the plugin stops wherever this check shows."
    ),
    ("stopped", LIMIT): (
        "stopped: the site says you've reached its limit on applications, so nothing was filled. Try this job again "
        "later."
    ),
    ("stopped", CANT_SET): (
        "stopped: the form wouldn't let the plugin set any box. Fill it in yourself in the open tab."
    ),
    ("stopped", UNEXPECTED_PAGE): (
        "stopped: the page didn't look like this job's form, so nothing was filled. Check the tab, then press Open and "
        "fill again."
    ),
    ("stopped", TOO_MANY_FIELDS): (
        "stopped: the page had more boxes than a form has, so nothing was filled. Check the tab, then press Open and "
        "fill again."
    ),
    ("stopped", NOTHING_TO_FILL): "nothing went to the browser: none of your answers could go on this form.",
    ("ended", BROWSER_REMOVED): "ended when its browser was removed",
    ("ended", REPLACED): "ended by a newer press",
    ("ended", SUBMITTED): "ended when you pressed I submitted it",
    ("ended", LOGGED_OUT): "ended when you logged out all devices",
    ("ended", GONE): "ended when the form's tab closed or moved on, before your answers went out",
}
LEFT = {  # why the plugin left a line, as the job's box says it
    "no-box": "the plugin couldn't find this box",
    "custom": "this drop-down is yours to pick",
    "place": "pick this place from the site's list yourself",
    "no-choice": "the plugin couldn't tell which choice is yours",
    "cant-set": "the form wouldn't let the plugin set this",
    "no-file": "the PDF couldn't be fetched",
    "file-refused": "the box doesn't show the PDF: check it",
    "didnt-stick": "the form shows something else here: check it",
    "not-reached": "the fill stopped before this one",
}


def latest(conn: sqlcipher3.Connection, job_id: int) -> dict | None:
    """The job's newest fill, in plain words; None before its first."""
    row = conn.execute(LATEST, (job_id,)).fetchone()
    return _plain(row) if row else None


def recent(conn: sqlcipher3.Connection, limit: int = 20) -> list[dict]:
    """The newest fills of any job, in plain words, with each job's title and company. They outlive their browser."""
    return [
        _plain(row[:11]) | {"job": row[11], "title": row[12], "company": row[13]}
        for row in conn.execute(RECENT, (limit,))
    ]


def total(conn: sqlcipher3.Connection) -> int:
    """How many fills `recent` could show."""
    return conn.execute(ALL_FILLS).fetchone()[0]


def toolbar_fills(conn: sqlcipher3.Connection) -> int:
    """The toolbar fills of the last SIGN_DAYS, whatever became of them; one dated later by a moved clock counts."""
    since = datetime.fromisoformat(now()) - timedelta(days=SIGN_DAYS)
    return conn.execute(TOOLBAR_FILLS, (since.isoformat(timespec="seconds"),)).fetchone()[0]


def _plain(row: tuple) -> dict:
    started_on, toolbar, browser, state, stop_kind, changes, filled, files, taken_on, checked_on, clicked_on = row
    closed = stop_kind == NOT_SENT
    return {
        "when": _moment(started_on), "taken": _moment(taken_on), "read_at": _moment(checked_on),
        "looks_sent": clicked_on is not None and not closed, "out": state in ("sent", "filled") and not closed,
        "toolbar": toolbar, "browser": browser,
        "what": HAPPENED.get((state, stop_kind), state), "changes": json.loads(changes or "[]"),
        "filled": json.loads(filled or "[]"),
        "files": ", ".join(slot.replace("_", " ") for slot in json.loads(files or "[]")),
    }  # fmt: skip


def _moment(stamp: str | None) -> str | None:
    if stamp is None:
        return None
    moment = datetime.fromisoformat(stamp).astimezone()  # the laptop's own clock
    return f"{moment.day} {moment:%b %H:%M}"
