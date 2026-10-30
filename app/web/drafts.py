"""What the Writer made, kept apart from main.py and prepare.py: a job's state on the Ready pages, its tailored PDF,
building that PDF again, the "changes vs your master" page, and the Usage page."""

import json
from collections.abc import Callable
from pathlib import Path

import sqlcipher3
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from app.config import ConfigError, SpendingSettings, load_config
from app.council import runs, spending, writer
from app.documents import pdf, privacy
from app.resume import ResumeError
from app.web import queries

SHOWN = {"day": 60, "week": 26, "month": 24, "year": None}  # how many of each the Usage page lists; None: all
VERSION = "SELECT id, version, status, problem, sources, draft, flags, writer, created_on FROM preparations "
VERSION += "WHERE job_id = ? AND kind = 'resume' AND version = ?"


def state(conn: sqlcipher3.Connection, data_dir: Path, preparer, job_id: int) -> dict:
    """What the Ready page and the job's box show: the newest version, the Writer's place, what the PDF build found,
    the cost so far, and whether Prepare would write a new version."""
    latest = runs.latest(conn, job_id)
    done = latest is not None and latest["status"] == "done"
    return {
        "latest": latest, "place": preparer.place(job_id), "outdated": done and runs.outdated(conn, job_id, latest),
        "built": pdf.built(pdf.tailored(data_dir, job_id, latest["version"])) if done else None,
        "cost": spending.of_job(conn, job_id), "writable": runs.writable(conn, job_id, latest),
    }  # fmt: skip


def add_routes(app: FastAPI, page: Callable[..., HTMLResponse], data_dir: Path) -> None:
    @app.get("/documents/{job_id:int}/resume-v{version:int}.pdf")
    async def tailored_pdf(job_id: int, version: int) -> Response:
        path = pdf.tailored(data_dir, job_id, version)  # whole numbers only: never a path from the address bar
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="application/pdf", filename=path.name, content_disposition_type="inline")

    @app.post("/jobs/{job_id:int}/pdf")
    async def build_again(request: Request, job_id: int) -> Response:
        """The newest version's PDF again, e.g. after a failed build or a changed Questionnaire contact."""
        conn, latest = request.app.state.conn, runs.latest(request.app.state.conn, queries.known(job_id))
        if latest is None or latest["status"] != "done":
            raise HTTPException(404)
        try:
            target, source = runs.pdf_source(conn, load_config(), latest["id"])
            await run_in_threadpool(pdf.build_kept, data_dir, target, source)  # up to minutes the first time
            runs.note_pdf(conn, latest["id"], None)
        except (pdf.PdfError, privacy.PrivacyError, ResumeError, ConfigError) as error:
            runs.note_pdf(conn, latest["id"], error)
        return RedirectResponse(f"/jobs/{job_id}", 303)

    @app.get("/jobs/{job_id:int}/changes")
    async def changes(request: Request, job_id: int) -> Response:
        """One version beside your master: each entry's original wording and the Writer's, new numbers marked."""
        conn, number = request.app.state.conn, request.query_params.get("version", "")
        rows = conn.execute(
            VERSION, (queries.known(job_id), int(number) if queries.PAGE_NUMBER.fullmatch(number) else 0)
        )
        found = rows.fetchone()
        if found is None or found[5] is None:  # no such version, or no draft
            raise HTTPException(404)
        version = dict(zip([column[0] for column in rows.description], found, strict=True))
        entries = runs.originals(conn, version["sources"])
        try:
            restore = runs.shield_for(conn, load_config(), entries).restore
        except (privacy.PrivacyError, ResumeError, ConfigError):  # shown as the Writer saw it, placeholders and all
            restore = str
        return page(
            request, "changes.html", job=queries.job(conn, job_id), version=version,
            rows=_rows(entries, json.loads(version["draft"]), json.loads(version["flags"]), restore),
            notes=json.loads(version["draft"])["notes"], model=json.loads(version["writer"])["model"],
            built=pdf.built(pdf.tailored(data_dir, job_id, version["version"])),
        )  # fmt: skip

    @app.get("/usage")
    async def usage(request: Request) -> Response:
        """Your AI use by day, week, month or year, and today's and this month's spend against your limits."""
        conn, by = request.app.state.conn, request.query_params.get("by", "day")
        by = by if by in SHOWN else "day"
        try:
            limits = load_config().spending
        except ConfigError:  # shown on the Settings page; the defaults meanwhile
            limits = SpendingSettings()
        return page(
            request, "usage.html", by=by, periods=list(SHOWN), rows=spending.history(conn, by)[: SHOWN[by]],
            spent=spending.spent(conn, spending.today()), limits=limits,
        )  # fmt: skip


def _rows(entries: list[writer.Entry], draft: dict, flags: list[dict], restore: Callable[[str], str]) -> list[dict]:
    """Each entry of the version's sources in your resume's order: its original and what the Writer made of it
    (None: left out), each new bullet in parts, the new numbers marked."""
    chosen = {(item["source"], item["tile_id"]): item for item in draft["entries"]}
    marked = {(flag["source"], flag["tile_id"], flag["bullet"]): set(flag["numbers"]) for flag in flags}
    rows = []
    for entry in entries:
        key, tile = (entry.source, entry.tile.tile_id), entry.tile
        item = chosen.get(key)
        bullets = [
            {"parts": _parts(restore(bullet["text"]), marked.get((*key, number), set())), "from": bullet["from"]}
            for number, bullet in enumerate(item["bullets"] if item else [])
        ]
        rows.append({
            "tile": tile, "chosen": item is not None, "bullets": bullets, "as_written": writer.kept_as_written(tile),
            "skills": [restore(skill) for skill in item["skills"]] if item else [],
        })  # fmt: skip
    return rows


def _parts(text: str, numbers: set[str]) -> list[tuple[str, bool]]:
    """The text in pieces, each new number a piece of its own marked True (the page highlights it)."""
    parts, last = [], 0
    for match in writer.NUMBER.finditer(text):
        if match[0] in numbers:
            parts += [(text[last : match.start()], False), (match[0], True)]
            last = match.end()
    return [part for part in [*parts, (text[last:], False)] if part[0]]
