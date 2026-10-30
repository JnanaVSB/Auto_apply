"""What the Writer works from: your Resume Profile and / or uploaded files (app/documents/uploads.py), as a default for
every job that a job can change. Prepare needs every chosen source's entries confirmed (app/documents/tile_sets.py)."""

import json

import sqlcipher3

from app.documents import tile_sets, uploads

PROFILE = "profile"  # your Resume Profile (resume.tex); an uploaded file is its id
SAVE_DEFAULT = """INSERT INTO writer_sources (id, sources) VALUES (1, ?)
    ON CONFLICT (id) DO UPDATE SET sources = excluded.sources"""


class SourcesError(Exception):
    """A choice that can't be saved."""


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
        changed = conn.execute(
            "UPDATE jobs SET writer_sources = ? WHERE id = ?", (json.dumps(sources) if sources else None, job_id)
        )
        return changed.rowcount == 1


def names(conn: sqlcipher3.Connection, sources: list) -> list[str]:
    files = {file.id: file.name for file in uploads.offered(conn)}
    return ["Profile" if source == PROFILE else files.get(source, "a removed file") for source in sources]


def unconfirmed(conn: sqlcipher3.Connection, sources: list, resume: str | None) -> list[str]:
    """The chosen sources whose entries aren't confirmed for their current text (your Profile: your current resume)."""
    files = {file.id: file for file in uploads.offered(conn)}
    missing = []
    for source, name in zip(sources, names(conn, sources), strict=True):
        text = resume if source == PROFILE else getattr(files.get(source), "fingerprint", None)
        confirmed = tile_sets.latest(conn, None if source == PROFILE else source)
        if text is None or confirmed is None or confirmed.resume != text:
            missing.append(name)
    return missing
