"""One shape for every site's application form (Phase 7), kept in the database: each field as the site words it,
public text only, never anything of yours. Each site's reader (app/submit/greenhouse.py, ...) builds it, and its
fingerprint tells a later read whether the form changed. Text is cut to the limits below, never refused."""

import hashlib
import json
from dataclasses import asdict, dataclass

import sqlcipher3

from app.collect import now
from app.resume import CONTROL

KINDS = ("text", "long_text", "single", "multi", "file", "location", "consent", "hidden", "unknown")
PARTS = ("standard", "company", "location", "eeo", "demographic", "privacy", "education")
MAX_LABEL, MAX_HELP, MAX_CHOICE = 1_000, 5_000, 500  # characters
MAX_CHOICES, MAX_FIELDS = 1_000, 500
UNREAD = "{:,} of this form's questions could not be read: check the form on the site before you submit."
SAVE = """INSERT INTO forms (job_id, read_on, read_from, fields, fingerprint, limit_note, problem)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (job_id) DO UPDATE SET read_on = excluded.read_on, read_from = excluded.read_from,
        fields = excluded.fields, fingerprint = excluded.fingerprint, limit_note = excluded.limit_note,
        problem = excluded.problem"""
LOAD = "SELECT fields, limit_note, read_on, read_from, problem FROM forms WHERE job_id = ?"


class FormError(Exception):
    """A form that can't be read; the message says why in plain words, never with the page's text."""


class FormClosed(FormError):
    """The posting is gone: the site says it doesn't exist any more."""


@dataclass(frozen=True)
class Choice:
    label: str
    value: str  # what the site sends back for it

    def __post_init__(self) -> None:
        object.__setattr__(self, "label", _line(self.label, MAX_CHOICE))


@dataclass(frozen=True)
class Field:
    key: str  # the site's own field name
    label: str  # the question, as the form words it
    kind: str  # one of KINDS
    required: bool
    part: str  # one of PARTS
    help: str = ""
    choices: tuple[Choice, ...] = ()
    accept: tuple[str, ...] = ()  # an upload's file types, e.g. ".pdf"

    def __post_init__(self) -> None:
        object.__setattr__(self, "label", _line(self.label, MAX_LABEL))
        object.__setattr__(self, "help", CONTROL.sub("", self.help)[:MAX_HELP])
        object.__setattr__(self, "choices", tuple(self.choices[:MAX_CHOICES]))
        object.__setattr__(self, "accept", tuple(self.accept))


@dataclass(frozen=True)
class Form:
    fields: tuple[Field, ...]
    limit_note: str | None = None  # the company's application-limit words, if any

    def __post_init__(self) -> None:
        if len(self.fields) > MAX_FIELDS:
            raise FormError(f"The site sent far more fields than a form has (over {MAX_FIELDS:,}), so it was not read.")
        if self.limit_note is not None:
            object.__setattr__(self, "limit_note", _line(self.limit_note, MAX_HELP))


def fingerprint(form: Form) -> str:
    """Changes with anything in the form: a field, its wording, choices, file types or place, or the limit note."""
    text = json.dumps([_fields(form), form.limit_note], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def save(conn: sqlcipher3.Connection, job_id: int, form: Form, read_from: str, unread: int = 0) -> None:
    """Keep the newest read of the job's form, and how many of its questions could not be read (`unread`); the same
    form read again only changes when it was read."""
    fields, problem = json.dumps(_fields(form), ensure_ascii=False), UNREAD.format(unread) if unread else None
    with conn:
        conn.execute(SAVE, (job_id, now(), read_from, fields, fingerprint(form), form.limit_note, problem))


def load(conn: sqlcipher3.Connection, job_id: int) -> tuple[Form, str, str, str | None] | None:
    """The job's newest form read, when it was read, from where (api, page or browser) and what could not be read in
    it (else None); None before any read."""
    row = conn.execute(LOAD, (job_id,)).fetchone()
    if row is None:
        return None
    fields = tuple(
        Field(**item | {"choices": tuple(Choice(**choice) for choice in item["choices"])})
        for item in json.loads(row[0])
    )
    return Form(fields, row[1]), row[2], row[3], row[4]


def _fields(form: Form) -> list[dict]:
    return [asdict(field) for field in form.fields]


def _line(text: str, limit: int) -> str:
    """One line of plain text: no invisible characters, single spaces, at most `limit` characters."""
    return " ".join(CONTROL.sub("", text).split())[:limit]
