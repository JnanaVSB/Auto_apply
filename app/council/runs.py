"""A job's versions of its tailored resume and cover letter, their saved steps (one per AI call: app/council/runner.py
writes them, app/council/flow.py decides them) and what is made of them: the PDF with your details put back on this
computer only, the cost, the failed tries. Messages name the cause and the fix, never your text, the job ad or the
answer."""

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import sqlcipher3

from app import llm, questionnaire
from app.config import Config
from app.council import writer
from app.documents import latex, pdf, privacy, tile_sets, uploads, writer_sources
from app.resume import read_latex

KIND, LETTER = "resume", "cover_letter"  # each kind has its own versions
LABELS = {"writing": "writing", "done": "done", "by_hand": "check by hand", "stopped": "stopped"}  # as pages say them
FAILED = ("stopped", "by_hand")  # a version that failed: Retry writes a new one
JOB = "SELECT title, company, locations, description FROM jobs WHERE id = ?"
NEWEST = """SELECT id, version, status, problem, sources, draft, flags, feedback, writer, created_on, council, round,
    agreed, wording, mode FROM preparations
    WHERE job_id = ? AND kind = ? {} ORDER BY version DESC LIMIT 1"""
LATEST, LAST_DONE, NUMBERED = NEWEST.format(""), NEWEST.format("AND status = 'done'"), NEWEST.format("AND version = ?")
# NULL + a cost stays NULL: a step whose price isn't known stays "not counted"
ENDED = """UPDATE steps SET status = ?, input_tokens = input_tokens + ?, cached_tokens = cached_tokens + ?,
    output_tokens = output_tokens + ?, cost = cost + ?, reserved = 0, answer = ?, error = ?, done_on = ? WHERE id = ?"""
FAILED_TRY = """INSERT INTO failed_tries (step_id, try_number, input_tokens, cached_tokens, output_tokens, cost, reason,
    reply, reply_length, saved_on) SELECT id, tries, ?, ?, ?, ?, ?, ?, ?, ? FROM steps WHERE id = ?"""
FAILED_TRIES = """SELECT f.saved_on, p.job_id, j.title, j.company, p.kind, p.version, s.model, f.try_number,
    f.input_tokens + f.cached_tokens, f.output_tokens, f.cost, f.reason, f.reply, f.reply_length
    FROM failed_tries f JOIN steps s ON s.id = f.step_id JOIN preparations p ON p.id = s.preparation_id
    JOIN jobs j ON j.id = p.job_id WHERE ?1 IS NULL OR p.id = ?1 ORDER BY f.id DESC LIMIT ?2 OFFSET ?3"""
JOB_WRITING = "SELECT 1 FROM preparations WHERE job_id = ? AND status = 'writing'"
VERSION = "SELECT job_id, kind, version, sources, draft, created_on, wording FROM preparations WHERE id = ?"
NOTE = "UPDATE preparations SET problem = ? WHERE id = ?"
VERSIONS = """SELECT p.id, p.version, p.status, p.created_on, p.feedback,
    coalesce(sum(coalesce(s.cost, 0) + s.reserved), 0), coalesce(max(s.id IS NOT NULL AND s.cost IS NULL), 0),
    (SELECT count(*) FROM failed_tries f JOIN steps t ON t.id = f.step_id WHERE t.preparation_id = p.id)
    FROM preparations p LEFT JOIN steps s ON s.preparation_id = p.id WHERE p.job_id = ? AND p.kind = ?
    GROUP BY p.id ORDER BY p.version DESC"""
ERROR = 300  # characters of a failed try's reason kept with its step
REPLY = 100_000  # characters of a failed try's reply kept for the Failed tries page
NO_BASE = "There is no finished version to change yet: press Prepare first."
CHANGED = "Your entries changed since version {}: press Prepare for a fresh version first, then ask for changes."
TOO_LONG = "Too long for the model ({}): raise the working size on the Writer settings page."
DETAIL_CHANGED = ("PDF built, but a bullet still shows {} you have changed since: update that entry on the Resume "
                  "Profile page, then press Prepare for a fresh version.")  # fmt: skip
DETAIL_KINDS = {
    "ME": "your name", "EMAIL": "an email address", "PHONE": "a phone number", "ADDRESS": "a street address",
    "LINK": "a link",
}  # fmt: skip


def latest(conn: sqlcipher3.Connection, job_id: int, kind: str = KIND, query: str = LATEST, *more: int) -> dict | None:
    """The job's newest version of its resume (or cover letter), or None."""
    rows = conn.execute(query, (job_id, kind, *more))
    row = rows.fetchone()
    return dict(zip([column[0] for column in rows.description], row, strict=True)) if row else None


def last_done(conn: sqlcipher3.Connection, job_id: int, kind: str = KIND) -> dict | None:
    """The job's newest finished version: what Request changes builds on."""
    return latest(conn, job_id, kind, LAST_DONE)


def version(conn: sqlcipher3.Connection, job_id: int, number: int) -> dict | None:
    """One version of the job's resume, by its number, or None."""
    return latest(conn, job_id, KIND, NUMBERED, number)


def versions(conn: sqlcipher3.Connection, job_id: int, kind: str = KIND) -> list[dict]:
    """Every version of the job's resume, newest first, with what it cost (and whether some was not counted) and how
    many of its tries failed."""
    names = ("id", "version", "status", "created_on", "feedback", "cost", "uncounted", "failed")
    return [dict(zip(names, row, strict=True)) for row in conn.execute(VERSIONS, (job_id, kind))]


def failed_tries(conn: sqlcipher3.Connection, limit: int, offset: int, version: int | None = None) -> list[dict]:
    """The Writer's failed tries, newest first (only one version's, if given), each with what came back."""
    names = ("saved_on", "job_id", "title", "company", "kind", "version", "model", "try_number", "read", "written",
             "cost", "reason", "reply", "reply_length")  # fmt: skip
    return [dict(zip(names, row, strict=True)) for row in conn.execute(FAILED_TRIES, (version, limit, offset))]


def writing(conn: sqlcipher3.Connection, job_id: int) -> bool:
    """Whether a version of the job (resume or cover letter) is being written."""
    return conn.execute(JOB_WRITING, (job_id,)).fetchone() is not None


def writable(version: dict | None, current: list[dict]) -> bool:
    """Whether Prepare writes a new version: none yet, the last one stopped or needs checking by hand (Retry), or it
    was made from older entries. Never while one is being written, nor twice from the same entries."""
    if version is None or version["status"] in FAILED:
        return True
    return version["status"] == "done" and outdated(version, current)


def current(conn: sqlcipher3.Connection, job_id: int) -> list[dict]:
    """The job's sources, each with its latest confirmed set: what a new version is made from."""
    return [{"source": source, "set": _set(conn, source)} for source in writer_sources.of_job(conn, job_id)[0]]


def outdated(version: dict, current: list[dict]) -> bool:
    """Whether a version was made from other entries than the job's sources have now (or from other sources)."""
    return json.loads(version["sources"]) != current


def pdf_source(conn: sqlcipher3.Connection, config: Config, preparation: int) -> tuple[Path, str, str | None]:
    """Where a version's tailored PDF goes, its LaTeX, and a note for the built PDF (else None). The LaTeX: your
    original fixed facts, the Writer's bullets and skills with your details put back (on this computer only), and the
    Questionnaire's contact header as it is now. A cover letter: that header, the version's date, the job, and the
    Writer's paragraphs. With no agreement and your original wording chosen, the entries the Truth checker objected to
    keep your original wording, and a letter's objected paragraphs are left out (app/council/flow.py)."""
    job_id, kind, number, sources, draft, created_on, wording = conn.execute(VERSION, (preparation,)).fetchone()
    draft = json.loads(draft)
    objected = draft.get("objected", []) if wording == "original" else []
    contact, target = questionnaire.load(conn)[0]["contact"], pdf.tailored(config.data_dir, job_id, number, kind)
    if kind == LETTER:
        title, company, _, _ = conn.execute(JOB, (job_id,)).fetchone()
        day = datetime.fromisoformat(created_on).astimezone()
        paragraphs = [text for number, text in enumerate(draft["paragraphs"]) if number not in objected]
        text = latex.cover_letter(contact, title, company, f"{day:%B} {day.day}, {day.year}", paragraphs)
        return target, text, None
    found = originals(conn, sources)
    restore, note = put_back(conn, config, found, draft, contact)
    by_id = {(entry.source, entry.tile.tile_id): entry.tile for entry in found}
    kept = {tuple(key) for key in objected}
    tiles = [by_id[key] if key in kept else _tailored(by_id[key], chosen, restore)
             for chosen in draft["entries"] if (key := (chosen["source"], chosen["tile_id"]))]  # fmt: skip
    return target, latex.resume(contact, tiles), note


def put_back(
    conn: sqlcipher3.Connection, config: Config, entries: list[writer.Entry], draft: dict, contact: dict
) -> tuple[Callable[[str], str], str | None]:
    """How a draft's placeholders are put back (on this computer only): each with the exact text it stood for when the
    draft was written, kept with it, so a later change to your details can't put a different link or name in its
    place; and a note when a bullet still shows a detail you have changed since (today's filter no longer knows it as
    one of your details). A draft from before the details were kept: with today's, as then."""
    today = shield_for(conn, config, entries, contact)
    if "details" not in draft:
        return today.restore, None
    changed = [DETAIL_KINDS[placeholder[1:-1].split("-")[0]]
               for placeholder, value in draft["details"].items()
               if not privacy.PLACEHOLDER.fullmatch(today.hide(value))]  # fmt: skip
    note = DETAIL_CHANGED.format(" and ".join(dict.fromkeys(changed))) if changed else None
    return partial(privacy.restore, details=draft["details"]), note


def note_pdf(conn: sqlcipher3.Connection, preparation: int, note: Exception | str | None) -> None:
    """A failed build keeps the draft and says why (the kind of error and its line, never your text); a built one keeps
    its note, if any."""
    with conn:
        conn.execute(NOTE, (f"PDF could not be built: {note}" if isinstance(note, Exception) else note, preparation))


def originals(conn: sqlcipher3.Connection, sources: str) -> list[writer.Entry]:
    """The entries a version was made from (its recorded sets, as JSON), in the order of your resume."""
    return writer.in_order([(found["source"], tile_sets.tiles(conn, found["set"])) for found in json.loads(sources)])


def shield_for(
    conn: sqlcipher3.Connection, config: Config, entries: list[writer.Entry], contact: dict
) -> privacy.Shield:
    """The privacy filter for these entries and their sources (and your Questionnaire's contact details), as the
    Writer's text is hidden with."""
    sources = list(dict.fromkeys(entry.source for entry in entries))
    latex_texts = [read_latex(config.data_dir) if source == writer_sources.PROFILE else uploads.latex(conn, source)
                   for source in sources]  # fmt: skip
    return privacy.shield(contact, latex_texts, [entry.tile for entry in entries])


def _tailored(tile, chosen: dict, restore: Callable[[str], str]):
    """One tile as the Writer chose it: its fixed facts unchanged, its bullets and skills the draft's."""
    skills = {"skills": [restore(skill) for skill in chosen["skills"]]} if tile.kind == "skills" else {}
    return replace(tile, facts=tile.facts | skills, bullets=tuple(restore(b["text"]) for b in chosen["bullets"]))


def ended(
    conn: sqlcipher3.Connection, step: int, status: str, usage: llm.Usage, cost: float | None,
    error: Exception | None, answer: dict | None = None, reply: str | None = None,
) -> None:  # fmt: skip
    """The call ended: its tokens and cost replace what was set aside. A failed try is also kept whole, with what came
    back: the answer your laptop refused (`reply`), or what the service sent with its error."""
    with conn:
        conn.execute(ENDED, (
            status, *usage, cost, json.dumps(answer, ensure_ascii=False) if answer else None,
            str(error)[:ERROR] if error else None, _now(), step,
        ))  # fmt: skip
        if status == "failed":
            came = reply if reply is not None else getattr(error, "reply", None)  # a keychain error has none
            cut = came is not None and len(came) > REPLY
            conn.execute(FAILED_TRY, (
                *usage, cost, str(error), came[:REPLY] if cut else came, len(came) if cut else None, _now(), step,
            ))  # fmt: skip


def _set(conn: sqlcipher3.Connection, source) -> int | None:
    return tile_sets.latest_id(conn, None if source == writer_sources.PROFILE else source)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
