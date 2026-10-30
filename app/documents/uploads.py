"""Your uploaded resumes and CVs (.tex), kept in the encrypted database. Their LaTeX is never run: it is only read into
entries (app/documents/tiles.py), and every PDF is built from the app's own template. Remove only stops a file being
offered; nothing is deleted. On the Resume Profile page each file is a source of entries beside your Profile."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlcipher3

from app.resume import MAX_BYTES, MIN_CHARACTERS, plain_text

MAX_FILES = 5  # per upload
MAX_NAME = 100
FIELD = re.compile(r"file\.([0-9])\.(name|latex)")  # one chosen file, as the page's script sends it
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
OFFERED = "SELECT id, name, latex, fingerprint, uploaded_on FROM resume_files WHERE removed_on IS NULL ORDER BY id"
SAVE = "INSERT INTO resume_files (name, latex, fingerprint, uploaded_on) VALUES (?, ?, ?, ?)"
REMOVE = "UPDATE resume_files SET removed_on = ? WHERE id = ? AND removed_on IS NULL"


class UploadError(Exception):
    """Files that can't be saved. Messages name the file, never its text."""


@dataclass(frozen=True)
class ResumeFile:
    id: int
    name: str
    latex: str
    fingerprint: str  # of its plain text, worked out as for resume.tex
    uploaded_on: str


def offered(conn: sqlcipher3.Connection) -> list[ResumeFile]:
    return [ResumeFile(*row) for row in conn.execute(OFFERED)]


def get(conn: sqlcipher3.Connection, file_id: int) -> ResumeFile | None:
    return next((file for file in offered(conn) if file.id == file_id), None)


def upload(conn: sqlcipher3.Connection, form: dict[str, str]) -> int:
    """Save the chosen files, all or nothing, and return how many; every problem is named at once."""
    chosen: dict[int, dict[str, str]] = {}
    for name, value in form.items():
        if match := FIELD.fullmatch(name):
            chosen.setdefault(int(match[1]), {})[match[2]] = value
    if not chosen:
        raise UploadError("Choose one or more .tex files first.")
    if len(chosen) > MAX_FILES:
        raise UploadError(f"Upload at most {MAX_FILES} files at a time.")
    current = offered(conn)
    names, prints = {file.name.lower() for file in current}, {file.fingerprint: file.name for file in current}
    saved, problems = [], []
    for _, file in sorted(chosen.items()):
        name, latex = " ".join(file.get("name", "").split()), file.get("latex", "").replace("\x00", "")
        tex = name.lower().endswith(".tex")
        name = name[:-4].rstrip() if tex else name
        if not name or len(name) > MAX_NAME or CONTROL.search(name):
            problems.append(f"a file name must be 1 to {MAX_NAME} visible characters")
        elif not tex:
            problems.append(f"{name}: only .tex files can be uploaded")
        elif len(latex.encode()) > MAX_BYTES:  # before reading it
            problems.append(f"{name}: is over {MAX_BYTES // 2**20} MB, which does not look like a resume")
        elif latex.count("\ufffd") > len(latex) // 100:  # what the browser makes of a PDF or Word file
            problems.append(f"{name}: doesn't look like a LaTeX text file (too many unreadable characters)")
        elif len(text := plain_text(latex)) < MIN_CHARACTERS:
            problems.append(f"{name}: looks empty (almost no text after removing LaTeX)")
        elif name.lower() in names:
            problems.append(f"{name}: a file with this name is already uploaded; remove it first, or rename this file")
        elif (fingerprint := hashlib.sha256(text.encode()).hexdigest()) in prints:
            problems.append(f"{name}: already uploaded as {prints[fingerprint]}")
        else:
            names.add(name.lower())
            prints[fingerprint] = name
            saved.append((name, latex, fingerprint, datetime.now(UTC).isoformat(timespec="seconds")))
    if problems:
        raise UploadError("; ".join(problems))
    with conn:  # all or nothing
        conn.executemany(SAVE, saved)
    return len(saved)


def remove(conn: sqlcipher3.Connection, file_id: int) -> bool:
    """Stop offering a file (it is kept), and take it out of the Writer's choices (app/documents/writer_sources.py):
    a job left with nothing uses your default, and the default is never left empty."""
    with conn:
        if conn.execute(REMOVE, (datetime.now(UTC).isoformat(timespec="seconds"), file_id)).rowcount != 1:
            return False
        row = conn.execute("SELECT sources FROM writer_sources").fetchone()
        if row and file_id in (sources := json.loads(row[0])):
            kept = [source for source in sources if source != file_id] or ["profile"]  # never an empty choice
            conn.execute("UPDATE writer_sources SET sources = ?", (json.dumps(kept),))
        for job_id, raw in conn.execute(
            "SELECT id, writer_sources FROM jobs WHERE writer_sources IS NOT NULL"
        ).fetchall():
            if file_id in (sources := json.loads(raw)):
                kept = [source for source in sources if source != file_id]
                conn.execute(
                    "UPDATE jobs SET writer_sources = ? WHERE id = ?", (json.dumps(kept) if kept else None, job_id)
                )
    return True
