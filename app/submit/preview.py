"""A job's saved form matched to your answers (Phase 7), as the job box, the Ready page and the Writer all see it, and
the answers you give for one job (form_answers, given by you)."""

from dataclasses import dataclass, replace
from pathlib import Path

import sqlcipher3

from app import questionnaire
from app.collect import now
from app.submit import Field, Form, ashby, load, match, record, wordings

KEPT = "SELECT field, answer, given_by, wording FROM form_answers WHERE job_id = ?"
YOURS = """INSERT INTO form_answers (job_id, field, answer, given_by, preparation_id, saved_on, wording)
    VALUES (?, ?, ?, 'you', NULL, ?, ?) ON CONFLICT (job_id, field) DO UPDATE SET answer = excluded.answer,
    given_by = 'you', preparation_id = NULL, saved_on = excluded.saved_on, wording = excluded.wording"""
FORGET = "DELETE FROM form_answers WHERE job_id = ? AND field = ? AND given_by = 'you'"


@dataclass(frozen=True)
class Preview:
    """The job's saved form, matched to your answers: what the part and the Ready page's badge show."""

    read_on: str
    read_from: str
    problem: str | None  # questions that could not be read
    lines: tuple[match.Line, ...]
    warnings: tuple[str, ...]
    open: int  # decisions required, suggestions included
    no_writer: bool  # the form has a rule about AI


def preview(conn: sqlcipher3.Connection, data_dir: Path, job_id: int) -> Preview | None:
    """The job's form as it would be filled in now; None before it is read."""
    saved = load(conn, job_id)
    if saved is None:
        return None
    form, read_on, read_from, problem = saved
    found, warnings, no_writer = lines(conn, form, job_id, record.documents(conn, data_dir, job_id))
    if read_from == "browser":
        found = tuple(map(held_back, found))
    still_open = sum(line.status in ("decide", "suggested") for line in found)
    return Preview(read_on, read_from, problem, found, warnings, still_open, no_writer)


def held_back(line: match.Line) -> match.Line:
    """A line of a form the page wrote, where a compare only checks the page against itself: the app's answer waits for
    your decision, unless it is your own or goes in one of the site's own boxes."""
    if line.status != "fill" or line.source == match.MINE or line.field.key in ashby.OWN_KEYS:
        return line
    if line.field.kind in ("file", "consent"):
        decision = "choose_file" if line.field.kind == "file" else "consent"
        return replace(line, status="decide", decision=decision, value="", source="")
    return replace(line, status="suggested")


def lines(
    conn: sqlcipher3.Connection, form: Form, job_id: int, files: dict[str, tuple[str, Path] | None] | None = None
) -> tuple[tuple[match.Line, ...], tuple[str, ...], bool]:
    """The form's lines for this job, its warnings, and whether it has a rule about AI."""
    saved = questionnaire.load(conn)[0]
    company, us_location = conn.execute("SELECT company, us_location FROM jobs WHERE id = ?", (job_id,)).fetchone()
    kept = {row[0]: row[1:] for row in conn.execute(KEPT, (job_id,))}
    found, warnings = match.match(
        form, saved, company, kept=kept, consents=saved.get("consents") or {}, files=files,
        us_job=us_location == "yes",
    )  # fmt: skip
    return found, warnings, wordings.no_writer(form, company)


def keep(conn: sqlcipher3.Connection, job_id: int, field: Field, answer: str) -> None:
    """Your answer for this job's question as it is worded now, replacing a Writer draft."""
    with conn:
        conn.execute(YOURS, (job_id, field.key, answer, now(), wordings.wording(field)))


def forget(conn: sqlcipher3.Connection, job_id: int, key: str) -> None:
    """Your answer for this job's question is gone, so the rules answer it again."""
    with conn:
        conn.execute(FORGET, (job_id, key))
