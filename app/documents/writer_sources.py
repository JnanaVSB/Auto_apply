"""What the Writer works from: your Resume Profile and / or uploaded files (app/documents/uploads.py), as a default for
every job that a job can change. Prepare needs every chosen source's entries confirmed (app/documents/tile_sets.py)."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlcipher3

from app.documents import tile_sets, uploads

PROFILE = "profile"  # your Resume Profile (resume.tex); an uploaded file is its id
SAVE_DEFAULT = """INSERT INTO writer_sources (id, sources) VALUES (1, ?)
    ON CONFLICT (id) DO UPDATE SET sources = excluded.sources"""
SAVE_JOB = "UPDATE jobs SET writer_sources = ? WHERE id = ?"


class SourcesError(Exception):
    """A choice that can't be saved."""


@dataclass(frozen=True)
class Source:
    name: str
    confirmed: bool  # its entries are confirmed for its current text


REMOVED = Source("a removed file", False)


def default(conn: sqlcipher3.Connection) -> list:
    row = conn.execute("SELECT sources FROM writer_sources").fetchone()
    return json.loads(row[0]) if row else [PROFILE]


def of_job(conn: sqlcipher3.Connection, job_id: int) -> tuple[list, bool]:
    """The job's sources, and whether they are its own choice (else your default)."""
    row = conn.execute("SELECT writer_sources FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return (json.loads(row[0]), True) if row and row[0] else (default(conn), False)


def ticked(conn: sqlcipher3.Connection, form: dict[str, str]) -> list:
    """The ticked boxes (source.profile, source.<file id>): offered files only, and at least one."""
    files = [file.id for file in uploads.offered(conn)]
    chosen = [PROFILE] * bool(form.get("source.profile")) + [file for file in files if form.get(f"source.{file}")]
    if not chosen:
        raise SourcesError("Tick at least one: your Profile or an uploaded file.")
    return chosen


def save_default(conn: sqlcipher3.Connection, sources: list) -> None:
    with conn:
        conn.execute(SAVE_DEFAULT, (json.dumps(sources),))


def save_job(conn: sqlcipher3.Connection, job_id: int, sources: list | None) -> bool:
    """The job's own choice; None goes back to your default."""
    with conn:
        return conn.execute(SAVE_JOB, (json.dumps(sources) if sources else None, job_id)).rowcount == 1


def offered(conn: sqlcipher3.Connection, resume: str | None) -> dict:
    """Every source you can choose, by its key (PROFILE or a file's id), loaded once per page for names() and
    unconfirmed(); `resume` is your current resume's fingerprint (None: no usable resume.tex)."""
    confirmed = tile_sets.confirmed_for(conn)
    texts = [(PROFILE, "Profile", resume)] + [(file.id, file.name, file.fingerprint) for file in uploads.offered(conn)]
    return {
        key: Source(name, text is not None and confirmed.get(None if key == PROFILE else key) == text)
        for key, name, text in texts
    }


def names(offered: dict, sources: list) -> list[str]:
    return [offered.get(source, REMOVED).name for source in sources]


def unconfirmed(offered: dict, sources: list) -> list[str]:
    """The chosen sources whose entries aren't confirmed for their current text (your Profile: your current resume)."""
    return [found.name for found in (offered.get(source, REMOVED) for source in sources) if not found.confirmed]


def remove(conn: sqlcipher3.Connection, file_id: int) -> bool:
    """Stop offering an uploaded file (it is kept) and take it out of every choice: a job left with nothing uses
    your default, and the default is never left empty."""
    with conn:
        if conn.execute(uploads.REMOVE, (datetime.now(UTC).isoformat(timespec="seconds"), file_id)).rowcount != 1:
            return False
        if file_id in (sources := default(conn)):
            conn.execute(SAVE_DEFAULT, (json.dumps([source for source in sources if source != file_id] or [PROFILE]),))
        for job_id, raw in conn.execute(
            "SELECT id, writer_sources FROM jobs WHERE writer_sources IS NOT NULL"
        ).fetchall():
            if file_id in (sources := json.loads(raw)):
                kept = [source for source in sources if source != file_id]
                conn.execute(SAVE_JOB, (json.dumps(kept) if kept else None, job_id))
    return True
