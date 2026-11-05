"""What you applied with (Phase 7): the copy panel's lines, and "I submitted it", which records what you did on the
site. The app never submits anything. A record is saved once, with the card's move to Applied, and is never changed;
a record made by hand holds what the app prepared, not what the site received. Nothing here contacts any site."""

import hashlib
import json
from datetime import date
from pathlib import Path

import sqlcipher3

from app import questionnaire, tracker
from app.collect import now
from app.council import runs
from app.documents import pdf
from app.submit import match
from app.tracker import STAGES

SAVE = """INSERT INTO submissions (job_id, submitted_on, saved_on, how, address, fields, files)
    VALUES (?, ?, ?, 'by hand', ?, ?, ?)"""
LOAD = "SELECT submitted_on, saved_on, how, address, fields, files FROM submissions WHERE job_id = ?"
NO_CARD = "This job has no card in Ready: use Add an application on the Tracker page, or press Prepare first"
BY_YOU, EMPTY_FROM, MISSING = "answered by you on the site", "left empty", "file was missing"
ALREADY = "Already recorded on {}"
NO_VALUE = {
    "decide": BY_YOU,
    "suggested": BY_YOU,
    "left": BY_YOU,
    "write": BY_YOU,
    "empty": EMPTY_FROM,
}  # -> its "from"
DOCUMENTS = ((runs.KIND, "Resume"), (runs.LETTER, "Cover letter"))
HEADING = {"browser": "What was sent", "by hand": "What the app prepared (you applied by hand)"}  # a record's `how`
MAY_DIFFER = "The site may hold different answers if you changed them there."
MOVED_BACK = "Your card was moved back on {}, after this record."


class Refused(Exception):  # noqa: N818 (shown as it is)
    """Nothing was recorded; the message says why in plain words, never with your text."""


class _Moved(Exception):
    """The card left Ready between the check and the move: the record rolls back with it."""


def standard(answers: dict) -> list[tuple[str, str]]:
    """The copy panel's lines (label, text), only those with a value."""
    contact = answers.get("contact") or {}
    lines = {
        "Name": contact.get("full_name"), "E-mail": contact.get("email"), "Phone": contact.get("phone"),
        "Location": questionnaire.place(contact), "LinkedIn": contact.get("linkedin"), "GitHub": contact.get("github"),
        "Portfolio": contact.get("portfolio"), **match.written(answers),
    }  # fmt: skip
    return [(label, text) for label, text in lines.items() if text]


def shown(line: match.Line) -> str:
    """A line's value as you'd read it: a choice's label (never its value), an upload's version (never its path)."""
    field = line.field
    if line.status not in ("fill", "suggested"):
        return ""
    if field.kind == "file":
        return line.source
    if field.kind == "consent" and line.value == match.TICKED:
        return "ticked"
    if isinstance(line.value, tuple):
        return ", ".join(field.labels.get(value, value) for value in line.value)
    return line.value


def documents(conn: sqlcipher3.Connection, data_dir: Path, job_id: int) -> dict[str, tuple[str, Path] | None]:
    """The job's newest finished resume and cover letter, each (label, PDF), or None without one or without its
    file."""
    found: dict[str, tuple[str, Path] | None] = {}
    for kind, name in DOCUMENTS:
        done = runs.last_done(conn, job_id, kind)
        path = pdf.tailored(data_dir, job_id, done["version"], kind) if done else None
        found[kind] = (f"{name} v{done['version']}", path) if path and path.is_file() else None
    return found


def recorded(conn: sqlcipher3.Connection, job_id: int) -> dict | None:
    """The job's record, with its fields and files read back; None before "I submitted it"."""
    row = conn.execute(LOAD, (job_id,)).fetchone()
    if row is None:
        return None
    submitted_on, saved_on, how, address, fields, files = row
    return {"submitted_on": submitted_on, "saved_on": saved_on, "how": how, "address": address,
            "fields": json.loads(fields), "files": json.loads(files)}  # fmt: skip


def submit(
    conn: sqlcipher3.Connection, data_dir: Path, job: dict, on: date, lines: tuple[match.Line, ...] | None
) -> str | None:
    """Record that you applied on `on`, and move the card from Ready to Applied, all or nothing; `lines` is the read
    form's preview (None: no form was read, so your standard answers are kept). Open decisions never stop it: they are
    kept as answered by you on the site. A card already past Ready (Gmail saw the confirmation first) stays where it
    is, never moved backwards, and that is said in the answer. Raises Refused with nothing saved."""
    job_id = job["id"]
    if found := recorded(conn, job_id):
        raise Refused(ALREADY.format(found["submitted_on"]))
    if not tracker.date_allowed(on, date.today()):
        raise Refused(tracker.BAD_DATE)
    stage = tracker.stage(conn, job_id)
    past_ready = stage not in (None, "ready")  # Gmail first, or Add an application: maybe with no resume version at all
    if not past_ready and (not tracker.on_board(conn, job_id) or runs.last_done(conn, job_id) is None):
        raise Refused(NO_CARD)
    fields = _fields(lines) if lines is not None else [
        {"question": label, "value": text, "from": "Questionnaire"}
        for label, text in standard(questionnaire.load(conn)[0])
    ]  # fmt: skip
    saved = (job_id, on.isoformat(), now(), job["apply_url"], json.dumps(fields, ensure_ascii=False),
             json.dumps(_files(conn, data_dir, job_id), ensure_ascii=False))  # fmt: skip
    try:
        with conn:
            conn.execute(SAVE, saved)
            if not past_ready and not tracker.moved(conn, job_id, "ready", "applied", on):
                raise _Moved
    except sqlcipher3.IntegrityError:  # a second dashboard saved its record after the check above
        if (found := recorded(conn, job_id)) is None:
            raise
        raise Refused(ALREADY.format(found["submitted_on"])) from None
    except _Moved:
        stage = STAGES[tracker.stage(conn, job_id) or "ready"]
        raise Refused(f"The card moved meanwhile: it is in {stage} now. Nothing was recorded: press I submitted it "
                      "again") from None  # fmt: skip
    return f"Recorded; the card stays in {STAGES[stage]}" if past_ready else None


def moved_back(conn: sqlcipher3.Connection, job_id: int, found: dict) -> str | None:
    """When the card last went back to Ready after the record was saved, else None. The record itself never changes."""
    later = (move for move in tracker.history(conn, job_id) if move["to_stage"] == "ready")
    return next((move["moved_on"] for move in later if move["moved_on"] >= found["saved_on"]), None)


def _fields(lines: tuple[match.Line, ...]) -> list[dict]:
    """Each question as the app prepared it. What you do yourself on the site is never guessed at: no value."""
    return [
        {"question": line.field.label, "value": "" if line.status in NO_VALUE else shown(line),
         "from": NO_VALUE.get(line.status, line.source),
         "note": line.note}
        for line in lines
        if line.status != "hidden"
    ]  # fmt: skip


def _files(conn: sqlcipher3.Connection, data_dir: Path, job_id: int) -> list[dict]:
    """The newest finished resume and cover letter, each with its SHA-256. A PDF gone from the folder is noted and
    never stops the record."""
    found = []
    for kind, name in DOCUMENTS:
        if (done := runs.last_done(conn, job_id, kind)) is None:
            continue
        version = done["version"]
        digest = _sha256(pdf.tailored(data_dir, job_id, version, kind))
        found.append({"slot": kind, "label": f"{name} v{version}", "version": version, "sha256": digest,
                      "note": "" if digest else MISSING})  # fmt: skip
    return found


def _sha256(path: Path) -> str:
    try:
        with path.open("rb") as file:  # read a piece at a time
            return hashlib.file_digest(file, "sha256").hexdigest()
    except OSError:
        return ""
