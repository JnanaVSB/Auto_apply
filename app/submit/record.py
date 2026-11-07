"""What you applied with (Phase 7): the copy panel's lines, and "I submitted it", which records what you did on the
site. The app never submits anything. A record is saved once, with the card's move to Applied, and is never changed;
a record made by hand holds what the app prepared, and one with the plugin's last read what the form showed: neither is
what the site received. Nothing here contacts any site."""

import hashlib
import json
from datetime import date
from pathlib import Path

import sqlcipher3

from app import questionnaire, tracker
from app.collect import now
from app.council import runs
from app.documents import pdf
from app.submit import fill_words, match
from app.tracker import STAGES

SAVE = """INSERT INTO submissions (job_id, submitted_on, saved_on, how, address, fields, files, checked_on)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)"""
LOAD = "SELECT submitted_on, saved_on, how, address, fields, files, checked_on FROM submissions WHERE job_id = ?"
LAST_READ = "SELECT values_read, checked_on, pdfs FROM fills WHERE job_id = ? ORDER BY id DESC LIMIT 1"
WIPED = "UPDATE fills SET values_read = NULL, checked_on = NULL WHERE job_id = ?"
NO_CARD = "This job has no card in Ready: use Add an application on the Tracker page, or press Prepare first"
BY_YOU, EMPTY_FROM, MISSING = "answered by you on the site", "left empty", "file was missing"
NOT_READ, AS_SHOWN = "the plugin couldn't read this box", "as the form showed it"
ANOTHER_FILE, NO_FILE_SHOWN = "the box showed another file", "its box showed no file when last checked"
MAX_VALUE = 20_000  # one answer as the form shows it
ALREADY = "Already recorded on {}"
NO_VALUE = {
    "decide": BY_YOU,
    "suggested": BY_YOU,
    "left": BY_YOU,
    "write": BY_YOU,
    "empty": EMPTY_FROM,
}  # -> its "from"
DOCUMENTS = ((runs.KIND, "Resume"), (runs.LETTER, "Cover letter"))
HEADING = {  # a record's `how`
    "browser": "What the form held when last checked ({})",
    "by hand": "What the app prepared (you applied by hand)",
}
MAY_DIFFER = "The site may hold different answers if you changed them there."
CAUTION = (
    "This is what the form showed the plugin, not proof of what the site received. Only questions the app knew about "
    "are here."
)
MOVED_BACK = "Your card was moved back on {}, after this record."
NO_COPY = "the app had no copy of this file when this was recorded: it can't show which PDF you sent"


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
        return line.note if line.source == match.MINE else line.source
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
    submitted_on, saved_on, how, address, fields, files, checked_on = row
    return {"submitted_on": submitted_on, "saved_on": saved_on, "how": how, "address": address,
            "fields": json.loads(fields), "files": json.loads(files), "checked_on": checked_on}  # fmt: skip


def heading(found: dict) -> str:
    """A record's heading, with when the form was last read if it holds a read."""
    return HEADING[found["how"]].format(fill_words.moment(found["checked_on"]))


def caution(found: dict) -> str:
    """What a record can't promise, under its heading."""
    return CAUTION if found["how"] == "browser" else MAY_DIFFER


def submit(
    conn: sqlcipher3.Connection, data_dir: Path, job: dict, on: date, lines: tuple[match.Line, ...] | None
) -> str | None:
    """Record that you applied on `on`, and move the card from Ready to Applied, all or nothing; `lines` is the read
    form's preview (None: no form was read, so your standard answers are kept). Open decisions never stop it: they are
    kept as answered by you on the site. If the job's newest fill holds a read and a form was read, the record is that
    read; every read of the job goes in the same save, whichever it was. A card already past Ready (Gmail saw the
    confirmation first) stays where it is, never moved backwards, and that is said in the answer. Raises Refused with
    nothing saved."""
    job_id = job["id"]
    if found := recorded(conn, job_id):
        raise Refused(ALREADY.format(found["submitted_on"]))
    if not tracker.date_allowed(on, date.today()):
        raise Refused(tracker.BAD_DATE)
    stage = tracker.stage(conn, job_id)
    past_ready = stage not in (None, "ready")  # Gmail first, or Add an application: maybe with no resume version at all
    if not past_ready and (not tracker.on_board(conn, job_id) or runs.last_done(conn, job_id) is None):
        raise Refused(NO_CARD)
    held = conn.execute(LAST_READ, (job_id,)).fetchone()
    read, checked_on = (json.loads(held[0]), held[1]) if held and held[0] and lines is not None else (None, None)
    handed = json.loads(held[2]) if read is not None and held[2] else {}
    paths = {str(found[1]): slot for slot, found in documents(conn, data_dir, job_id).items() if found}
    labels = {path: _label(slot, handed[slot]["version"]) for path, slot in paths.items() if slot in handed}
    fields = _fields(lines, read, labels) if lines is not None else [
        {"question": label, "value": text, "from": "Questionnaire"}
        for label, text in standard(questionnaire.load(conn)[0])
    ]  # fmt: skip
    files = _files(conn, data_dir, job_id, handed, _changed(paths, lines, read) if read else set())
    saved = (job_id, on.isoformat(), now(), "by hand" if read is None else "browser", job["apply_url"],
             json.dumps(fields, ensure_ascii=False), json.dumps(files, ensure_ascii=False), checked_on)  # fmt: skip
    try:
        with conn:
            conn.execute(SAVE, saved)
            conn.execute(WIPED, (job_id,))
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


def _fields(lines: tuple[match.Line, ...], read: dict | None = None, labels: dict | None = None) -> list[dict]:
    """Each question as the app prepared it, or with a read as the form last showed it, with `labels` for a PDF handed
    out, by its path. What you do yourself on the site is never guessed at: no value."""
    fields = []
    for line in lines:
        if line.status != "hidden":
            field = {"question": line.field.label, "value": "" if line.status in NO_VALUE else shown(line),
                     "from": NO_VALUE.get(line.status, line.source),
                     "note": line.note}  # fmt: skip
            fields.append(field if read is None else field | _as_read(line, field["value"], read, labels or {}))
    return fields


def _as_read(line: match.Line, prepared: str, read: dict, labels: dict) -> dict:
    """What the read changes in a line's row: nothing if it shows what the app prepared. A file goes by its verdict, as
    a read never holds another file's name."""
    key = line.field.key
    if key not in read:
        return {"value": "", "from": NOT_READ}
    if line.field.kind == "file":
        handed = labels.get(line.value)
        verdicts = {
            fill_words.SAME_FILE: {"value": handed, "from": handed} if handed else {},
            fill_words.CHANGED_FILE: {"value": ANOTHER_FILE, "from": AS_SHOWN},
            "": {"value": "", "from": NO_FILE_SHOWN},
        }
        said = read[key] if isinstance(read[key], str) else None  # a key can change to a file box after its read
        return verdicts.get(said, {"value": "", "from": NOT_READ})
    texts = [read[key]] if isinstance(read[key], str) else read[key]
    if not any(text.strip() for text in texts):
        return {"value": "", "from": EMPTY_FROM}
    showing = ", ".join(texts)
    if _plain(showing) == _plain(prepared):
        return {}
    return {"value": showing[:MAX_VALUE], "from": AS_SHOWN}


def _plain(text: str) -> str:
    return " ".join(text.split()).casefold()


def _label(slot: str, version: int) -> str:
    return f"{dict(DOCUMENTS)[slot]} v{version}"


def _changed(paths: dict[str, str], lines: tuple[match.Line, ...], read: dict) -> set[str]:
    """The slots whose file line reads as changed."""
    verdicts = ((line, read.get(line.field.key)) for line in lines if line.field.kind == "file")
    return {paths[line.value] for line, said in verdicts if said == fill_words.CHANGED_FILE and line.value in paths}


def _files(conn: sqlcipher3.Connection, data_dir: Path, job_id: int, handed: dict, changed: set[str]) -> list[dict]:
    """The resume and cover letter a read's fill handed out, else the newest finished ones, each with its SHA-256. A PDF
    gone from the folder is noted and never stops the record."""
    found = []
    for kind, _ in DOCUMENTS:
        if (done := handed.get(kind) or runs.last_done(conn, job_id, kind)) is None:
            continue
        version = done["version"]
        digest = done["sha256"] if kind in handed else sha256(pdf.tailored(data_dir, job_id, version, kind))
        found.append({"slot": kind, "label": _label(kind, version), "version": version, "sha256": digest,
                      "note": "" if digest else MISSING} | ({"changed": True} if kind in changed else {}))  # fmt: skip
    return found


def sha256(path: Path) -> str:
    try:
        with path.open("rb") as file:  # read a piece at a time
            return hashlib.file_digest(file, "sha256").hexdigest()
    except OSError:
        return ""
