"""Phase 4 pages, kept apart from main.py: the questionnaire, the tiles and Ready to Apply."""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import sqlcipher3
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from app import questionnaire
from app.config import ConfigError, load_config
from app.documents import latex, pdf, tile_sets
from app.documents.tiles import PREFIXES, header_text, read_tiles
from app.resume import ResumeError, from_latex, read_latex
from app.scoring import ready
from app.web import actions, queries

CHOICES = {  # the questionnaire's drop-down choices
    "STATUSES": questionnaire.STATUSES, "GENDERS": questionnaire.GENDERS, "RACES": questionnaire.RACES,
    "VETERAN": questionnaire.VETERAN, "DISABILITY": questionnaire.DISABILITY, "MAX_PAIRS": questionnaire.MAX_PAIRS,
}  # fmt: skip


RESUME_CHANGED = "Your resume changed while this page was open: reload the page (nothing was confirmed)."


TILES_FIRST = "Confirm your tiles first: the Writer works from your confirmed tiles."
NOT_READY = 'Only a ready job can be prepared (or one you marked "Prepare anyway" on its page).'


def add_routes(
    app: FastAPI, page: Callable[..., HTMLResponse], data_dir: Path, scoring: Callable[[], queries.Scoring]
) -> None:
    @app.get("/questionnaire")
    async def questionnaire_page(request: Request) -> Response:
        values, saved_on, problem = questionnaire.load(request.app.state.conn)
        return page(
            request, "questionnaire.html", **CHOICES, values=values, saved_on=saved_on, stale=problem,
            saved="saved" in request.query_params,
        )  # fmt: skip

    @app.post("/questionnaire")
    async def save_questionnaire(request: Request) -> Response:
        values = questionnaire.typed(request.state.form)
        try:
            questionnaire.save(request.app.state.conn, questionnaire.check(values))
        except questionnaire.QuestionnaireError as error:  # nothing saved; what was typed stays on the page
            return page(request, "questionnaire.html", **CHOICES, values=values, problem=str(error))
        return RedirectResponse("/questionnaire?saved=1", 303)  # so reloading the page never saves twice

    @app.get("/questionnaire/download")
    async def download_questionnaire(request: Request) -> Response:
        values, _, _ = questionnaire.load(request.app.state.conn)
        return Response(
            questionnaire.as_file(values), media_type="application/yaml; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="questionnaire.yaml"', "Cache-Control": "no-store"},
        )  # fmt: skip

    @app.get("/tiles")
    async def tiles_page(request: Request) -> Response:
        view = _tiles_view(request.app.state.conn, data_dir)
        notice = {
            "confirmed": "Tiles confirmed.", "unchanged": "Nothing changed, so nothing was saved.",
            "built": "Master PDF built.",
        }  # fmt: skip
        return page(
            request, "tiles.html", **view, notice=next((notice[n] for n in notice if n in request.query_params), None)
        )

    @app.post("/tiles")
    async def confirm_tiles(request: Request) -> Response:
        form, conn = request.state.form, request.app.state.conn
        rows, view = tile_sets.typed(form), _tiles_view(conn, data_dir)
        try:
            if view.get("resume") != form.get("resume"):
                raise tile_sets.TilesError(RESUME_CHANGED)
            set_id = tile_sets.confirm(conn, tile_sets.check(rows), view["resume"])
        except tile_sets.TilesError as error:  # nothing saved; what was typed stays on the page
            return page(
                request, "tiles.html", **view | {"rows": rows, "resume": form.get("resume")}, problem=str(error)
            )
        return RedirectResponse(f"/tiles?{'confirmed' if set_id else 'unchanged'}=1", 303)  # reloading never confirms

    @app.post("/tiles/pdf")
    async def build_master(request: Request) -> Response:
        """The master PDF: your confirmed tiles in your original wording, with the Questionnaire's contact header."""
        conn = request.app.state.conn
        view, contact = _tiles_view(conn, data_dir), questionnaire.load(conn)[0]["contact"]
        try:
            if not view.get("current"):
                raise pdf.PdfError("Confirm your tiles first: the master PDF is made from your confirmed tiles.")
            if not contact.get("full_name"):
                raise pdf.PdfError(pdf.NO_NAME)
            source = latex.resume(contact, view["confirmed"].tiles)
            await run_in_threadpool(pdf.build_master, data_dir, view["confirmed"].set_id, source)  # up to minutes
        except pdf.PdfError as error:
            return page(request, "tiles.html", **view, problem=str(error))
        return RedirectResponse("/tiles?built=1", 303)

    @app.get("/documents/master/resume-v{set_id:int}.pdf")
    async def master_pdf(set_id: int) -> Response:
        path = pdf.master(data_dir, set_id)  # a whole number only: never a path from the address bar
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="application/pdf", filename=path.name, content_disposition_type="inline")

    @app.get("/ready")
    async def ready_page(request: Request) -> Response:
        """The jobs ready for preparation (one rule, in app.scoring.ready), newest first."""
        conn, current, number = request.app.state.conn, scoring(), request.query_params.get("page", "1")
        view = queries.Filters(scoring=("ready",), page=int(number) if number.isascii() and number.isdigit() else 1)
        listing = queries.list_jobs(conn, view, current)
        ids = [job["id"] for job in listing.jobs]
        marks = ", ".join("?" * len(ids))
        queued = dict(conn.execute(f"SELECT id, prepare_requested_on FROM jobs WHERE id IN ({marks})", ids))  # noqa: S608
        skipped = request.query_params.get("skipped", "")
        return page(
            request, "ready.html", listing=listing, view=view, queued=queued, tiles_ok=_tiles_ok(conn, current),
            skipped=int(skipped) if skipped.isascii() and skipped.isdigit() else None,
            problem=PROBLEMS.get(request.query_params.get("problem", "")),
        )  # fmt: skip

    @app.post("/ready/{job_id:int}/prepare")
    async def prepare_job(request: Request, job_id: int) -> Response:
        conn, current = request.app.state.conn, scoring()
        job = queries.job(conn, job_id) if job_id <= MAX_JOB_ID else None
        if job is None:
            raise HTTPException(404)
        if not _tiles_ok(conn, current):
            problem = "tiles"
        elif job["status"] != "open" or queries.job_scoring(conn, job_id, current)["status"] != "ready":
            problem = "not-ready"
        else:
            ready.queue(conn, job_id, datetime.now(UTC).isoformat(timespec="seconds"))  # a second press: no change
            problem = None
        back = f"/jobs/{job_id}" if request.state.form.get("back") == "job" else "/ready"  # the page it was pressed on
        return RedirectResponse(f"{back}?problem={problem}" if problem else back, 303)

    @app.post("/ready/{job_id:int}/skip")
    async def skip_job(request: Request, job_id: int) -> Response:
        """Skip is your "Not interested" switch: the job is only hidden, and Undo brings it back."""
        value = request.state.form.get("value") == "1"
        if job_id > MAX_JOB_ID or not actions.set_not_interested(request.app.state.conn, job_id, value):
            raise HTTPException(404)
        return RedirectResponse(f"/ready?skipped={job_id}" if value else "/ready", 303)

    @app.get("/jobs/{job_id:int}/preparation")
    async def preparation_box(request: Request, job_id: int) -> Response:
        """The job page's Ready to Apply box, loaded by the page itself."""
        conn, current = request.app.state.conn, scoring()
        if job_id > MAX_JOB_ID or queries.job(conn, job_id) is None:
            raise HTTPException(404)
        queued = conn.execute("SELECT prepare_requested_on FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
        return page(
            request, "preparation.html", job_id=job_id, queued=queued, tiles_ok=_tiles_ok(conn, current),
            ready=queries.job_scoring(conn, job_id, current)["status"] == "ready",
        )  # fmt: skip


PROBLEMS = {"tiles": TILES_FIRST, "not-ready": NOT_READY}
MAX_JOB_ID = 2**63 - 1  # the largest number the database can hold


def _tiles_ok(conn: sqlcipher3.Connection, current: queries.Scoring) -> bool:
    """Whether your confirmed tiles were read from your current resume."""
    confirmed = tile_sets.latest(conn)
    return confirmed is not None and current.resume is not None and confirmed.resume == current.resume


def _tiles_view(conn: sqlcipher3.Connection, data_dir: Path) -> dict:
    """What the Tiles page shows: your confirmed tiles if made from the current resume, else a fresh reading."""
    try:
        latex = read_latex(data_dir)
        resume = from_latex(latex).fingerprint
    except ResumeError as error:
        return {"resume_problem": str(error), "rows": [], "kinds": PREFIXES}
    reading, confirmed = read_tiles(latex), tile_sets.latest(conn)
    current = confirmed is not None and confirmed.resume == resume
    tiles = confirmed.tiles if current else reading.tiles
    contact = questionnaire.load(conn)[0]["contact"]
    try:
        one_page = load_config().documents.one_page
    except ConfigError:  # shown on the Settings page; the default meanwhile
        one_page = True
    return {
        "resume": resume, "confirmed": confirmed, "current": current, "kinds": PREFIXES,
        "rows": [tile_sets.as_row(tile, position) for position, tile in enumerate(tiles, 1)],
        "changes": tile_sets.changes(confirmed.tiles, reading.tiles) if confirmed and not current else [],
        "unplaced": reading.unplaced, "reading_problem": reading.problem,
        "contact_empty": not any(contact.get(field) for field, _ in tile_sets.CONTACT),
        "contact_missing": tile_sets.contact_check(contact, header_text(latex)),
        "pdf": pdf.built(pdf.master(data_dir, confirmed.set_id)) if current else None, "one_page": one_page,
    }  # fmt: skip
