"""Phase 4 pages, kept apart from main.py: the questionnaire, the tiles and Ready to Apply.

Users see the tiles as the "Resume Profile" page (/resume-profile, template tiles.html) and a tile as an "entry"
(see app/documents/tiles.py); only the code keeps the word "tile"."""

from collections.abc import Callable, Collection
from datetime import UTC, datetime
from pathlib import Path

import sqlcipher3
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from app import questionnaire
from app.config import ConfigError, load_config
from app.council import flow, runs
from app.documents import latex, pdf, tile_sets, uploads, writer_sources
from app.documents.tiles import PREFIXES, header_text, read_tiles
from app.resume import CONTROL, ResumeError, from_latex, read_latex
from app.scoring import ready
from app.web import actions, drafts, queries

CHOICES = {  # the questionnaire's drop-down choices
    "STATUSES": questionnaire.STATUSES, "GENDERS": questionnaire.GENDERS, "RACES": questionnaire.RACES,
    "VETERAN": questionnaire.VETERAN, "DISABILITY": questionnaire.DISABILITY, "MAX_PAIRS": questionnaire.MAX_PAIRS,
}  # fmt: skip


RESUME_CHANGED = "Your resume changed while this page was open: reload the page (nothing was confirmed)."


TILES_FIRST = "Confirm the entries of every source this job uses first, on the Resume Profile page."
PROFILE = "/resume-profile"  # the tiles page's address
DOUBTS = {  # the job page's pop-up asks before the Writer works on such a job: you can proceed anyway
    "skipped": 'This job is skipped ("Not interested").',
    "closed": "This job is no longer open.",
    "not-ready": "This job is not ready (no score at or above your threshold with your current resume).",
}
ANYWAY = " Press OK to proceed anyway."
NO_WRITER = "Choose the Writer's model on the Writer settings page (Settings, then Writer settings) first."
NO_COUNCIL = "Choose the council's members in Settings first, or press Resume only for the Writer alone."
BUSY_JOB = "The Writer is still working on this job: try again when it's done."
MAX_FEEDBACK = 2000


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

    @app.get(PROFILE)
    async def tiles_page(request: Request) -> Response:
        conn = request.app.state.conn
        view = _tiles_view(conn, data_dir, _file(conn, request.query_params.get("file", "")))
        notice = {
            "confirmed": "Resume profile confirmed.", "unchanged": "Nothing changed, so nothing was saved.",
            "built": "Master PDF built.", "uploaded": "Uploaded: choose a file above to check and confirm its entries.",
            "removed": "Removed (it is kept, but no longer offered).", "sources": "Default choice saved.",
        }  # fmt: skip
        return page(
            request, "tiles.html", **view, notice=next((notice[n] for n in notice if n in request.query_params), None)
        )

    @app.post(PROFILE)
    async def confirm_tiles(request: Request) -> Response:
        form, conn = request.state.form, request.app.state.conn
        file = _file(conn, form.get("file", ""))
        rows, view = tile_sets.typed(form), _tiles_view(conn, data_dir, file)
        try:
            if view.get("resume") != form.get("resume"):
                raise tile_sets.TilesError(RESUME_CHANGED)
            set_id = tile_sets.confirm(conn, tile_sets.check(rows), view["resume"], file and file.id)
        except tile_sets.TilesError as error:  # nothing saved; what was typed stays on the page
            return page(
                request,
                "tiles.html",
                **view | {"rows": rows, "resume": form.get("resume")},
                problem=f"Not confirmed: {error}",
            )
        done = "confirmed" if set_id else "unchanged"
        source = f"file={file.id}&" if file else ""
        return RedirectResponse(f"{PROFILE}?{source}{done}=1", 303)  # so reloading the page never confirms again

    @app.post(f"{PROFILE}/upload")  # main.py lets only this address take a big form (the files' text)
    async def upload_files(request: Request) -> Response:
        conn = request.app.state.conn
        try:
            uploads.upload(conn, request.state.form)
        except uploads.UploadError as error:  # nothing saved
            return page(request, "tiles.html", **_tiles_view(conn, data_dir), problem=f"Not uploaded: {error}.")
        return RedirectResponse(f"{PROFILE}?uploaded=1", 303)

    @app.post(PROFILE + "/files/{file_id:int}/remove")
    async def remove_file(request: Request, file_id: int) -> Response:
        if not writer_sources.remove(request.app.state.conn, queries.known(file_id)):
            raise HTTPException(404)
        return RedirectResponse(f"{PROFILE}?removed=1", 303)

    @app.post(f"{PROFILE}/sources")
    async def save_default_sources(request: Request) -> Response:
        """What the Writer uses for every job that has no choice of its own."""
        conn = request.app.state.conn
        try:
            writer_sources.save_default(conn, writer_sources.ticked(conn, request.state.form))
        except writer_sources.SourcesError as error:
            return page(request, "tiles.html", **_tiles_view(conn, data_dir), problem=str(error))
        return RedirectResponse(f"{PROFILE}?sources=1", 303)

    @app.post(f"{PROFILE}/pdf")
    async def build_master(request: Request) -> Response:
        """The master PDF: your confirmed tiles in your original wording, with the Questionnaire's contact header."""
        conn = request.app.state.conn
        view, contact = _tiles_view(conn, data_dir), questionnaire.load(conn)[0]["contact"]
        try:
            if not view.get("current"):
                raise pdf.PdfError("Confirm your resume profile first: the master PDF is made from its entries.")
            if not contact.get("full_name"):
                raise pdf.PdfError(pdf.NO_NAME)
            source = latex.resume(contact, view["confirmed"].tiles)
            await run_in_threadpool(pdf.build_master, data_dir, view["confirmed"].set_id, source)  # up to minutes
        except pdf.PdfError as error:
            return page(request, "tiles.html", **view, problem=str(error))
        return RedirectResponse(f"{PROFILE}?built=1", 303)

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
        view = queries.Filters(
            scoring=("ready",), page=max(1, int(number)) if queries.PAGE_NUMBER.fullmatch(number) else 1
        )
        listing, offered = queries.list_jobs(conn, view, current), writer_sources.offered(conn, current.resume)
        ids = [job["id"] for job in listing.jobs]
        chosen = {job_id: writer_sources.of_job(conn, job_id)[0] for job_id in ids}
        uses = {job_id: writer_sources.names(offered, chosen[job_id]) for job_id in ids}
        missing = {job_id: writer_sources.unconfirmed(offered, chosen[job_id]) for job_id in ids}
        states = {job_id: drafts.state(conn, data_dir, job_id) for job_id in ids}
        skipped = request.query_params.get("skipped", "")
        return page(
            request, "ready.html", listing=listing, view=view, states=states, uses=uses, missing=missing,
            labels=runs.LABELS,
            skipped=int(skipped) if queries.PAGE_NUMBER.fullmatch(skipped) else None,
            problem=PROBLEMS.get(request.query_params.get("problem", "")),
        )  # fmt: skip

    @app.post("/ready/{job_id:int}/prepare")
    async def prepare_job(request: Request, job_id: int) -> Response:
        conn, current = request.app.state.conn, scoring()
        job = queries.job(conn, queries.known(job_id))
        if job is None:
            raise HTTPException(404)
        waived, alone = waived_of(request), request.state.form.get("writer_only") == "1"  # "Resume only"
        if not (problem := cannot(conn, current, job, waived, alone)):
            ready.queue(conn, job_id, datetime.now(UTC).isoformat(timespec="seconds"))  # a second press: no change
            latest = runs.latest(conn, job_id)
            if runs.writable(latest, runs.current(conn, job_id)):  # Retry a change: the same words
                failed = latest is not None and latest["status"] in runs.FAILED
                words = latest["feedback"] or "" if failed else ""
                flow.start(conn, load_config(), job_id, feedback=words, waived=waived, writer_only=alone)
                request.app.state.preparer.wake()
        if request.state.form.get("back") == "job":  # pressed in the job's box: the box comes back
            return box(request, job_id, prepare_problem=problem and PROBLEMS[problem])
        return RedirectResponse(f"/ready?problem={problem}" if problem else "/ready", 303)

    @app.post("/ready/{job_id:int}/skip")
    async def skip_job(request: Request, job_id: int) -> Response:
        """Skip is your "Not interested" switch: the job is only hidden, and Undo brings it back."""
        value = request.state.form.get("value") == "1"
        if not actions.set_not_interested(request.app.state.conn, queries.known(job_id), value):
            raise HTTPException(404)
        return RedirectResponse(f"/ready?skipped={job_id}" if value else "/ready", 303)

    @app.get("/jobs/{job_id:int}/preparation")
    async def preparation_box(request: Request, job_id: int) -> Response:
        """The job page's Ready to Apply box, loaded by the page itself."""
        return box(request, job_id)

    @app.post("/jobs/{job_id:int}/request-changes")
    async def request_changes(request: Request, job_id: int) -> Response:
        """Your words to the Writer: a new version built on the newest finished one, or, while one is being written,
        added to it for the Writer's next round (owner choice D). Answers with the job's box."""
        conn, preparer = request.app.state.conn, request.app.state.preparer
        words = " ".join(CONTROL.sub("", request.state.form.get("feedback", "")).split())
        job, waived = queries.job(conn, queries.known(job_id)), waived_of(request)
        if job is None:
            raise HTTPException(404)
        base, latest = runs.last_done(conn, job_id), runs.latest(conn, job_id)
        if refused := cannot(conn, scoring(), job, waived):
            problem = PROBLEMS[refused]
        elif not words or len(words) > MAX_FEEDBACK:
            problem = f"Type what you'd like changed (at most {MAX_FEEDBACK:,} characters)."
        elif latest is not None and latest["status"] == "writing" and runs.alone(latest):
            problem = runs.ALONE
        elif latest is not None and latest["status"] == "writing":
            flow.ask(conn, latest["id"], words)
            problem = None
        elif base is None:
            problem = runs.NO_BASE
        elif runs.outdated(base, runs.current(conn, job_id)):
            problem = runs.CHANGED.format(base["version"])
        else:
            flow.start(conn, load_config(), job_id, feedback=words, waived=waived)
            preparer.wake()
            problem = None
        return box(request, job_id, problem, words if problem else "")

    @app.post("/jobs/{job_id:int}/cover-letter")
    async def write_letter(request: Request, job_id: int) -> Response:
        """Write cover letter: a new letter version for this job, in the background. Answers with the job's box."""
        conn, preparer = request.app.state.conn, request.app.state.preparer
        job = queries.job(conn, queries.known(job_id))
        if job is None:
            raise HTTPException(404)
        waived = waived_of(request)
        if refused := cannot(conn, scoring(), job, waived):
            problem = PROBLEMS[refused]
        elif _busy(conn, job_id):
            problem = BUSY_JOB
        else:
            flow.start(conn, load_config(), job_id, runs.LETTER, waived=waived)
            preparer.wake()
            problem = None
        return box(request, job_id, letter_problem=problem)

    def box(
        request: Request, job_id: int, change_problem: str | None = None, typed: str = "",
        letter_problem: str | None = None, prepare_problem: str | None = None,
    ) -> HTMLResponse:  # fmt: skip
        conn, current = request.app.state.conn, scoring()
        job = queries.job(conn, queries.known(job_id))
        if job is None:
            raise HTTPException(404)
        queued = conn.execute("SELECT prepare_requested_on FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
        chosen, own = writer_sources.of_job(conn, job_id)
        preparer = request.app.state.preparer
        free, doubts = not _busy(conn, job_id), _doubts(conn, current, job)
        missing = writer_sources.unconfirmed(writer_sources.offered(conn, current.resume), chosen)
        writer = _writer_chosen()
        return page(
            request, "preparation.html", job_id=job_id, queued=queued, chosen=chosen, own=own,
            files=uploads.offered(conn), **drafts.state(conn, data_dir, job_id),
            versions=runs.versions(conn, job_id), labels=runs.LABELS, notice=preparer.notice, writer=writer,
            one_page=_one_page(), change_problem=change_problem, typed=typed, letter_problem=letter_problem,
            letter=drafts.letter(conn, data_dir, job_id), prepare_problem=prepare_problem,
            can_letter=free and not missing and writer, missing=missing, doubts=doubts,
            confirm=" ".join(DOUBTS[doubt] for doubt in doubts) + ANYWAY, **drafts.council(conn, job_id),
        )  # fmt: skip

    @app.post("/jobs/{job_id:int}/sources")
    async def save_job_sources(request: Request, job_id: int) -> Response:
        """The job's own choice of what the Writer uses, or back to your default."""
        conn, form = request.app.state.conn, request.state.form
        try:
            chosen = None if form.get("default") else writer_sources.ticked(conn, form)
        except writer_sources.SourcesError:
            chosen = None  # nothing ticked: the default
        if not writer_sources.save_job(conn, queries.known(job_id), chosen):
            raise HTTPException(404)
        return RedirectResponse(f"/jobs/{job_id}", 303)


PROBLEMS = {"tiles": TILES_FIRST, "writer": NO_WRITER, "council": NO_COUNCIL} | {
    doubt: f"{text} To proceed anyway, press the button on the job's page and confirm."
    for doubt, text in DOUBTS.items()
}  # a doubt refused: the press came from a page that didn't ask (the Ready page, or one shown before it changed)


def cannot(
    conn: sqlcipher3.Connection, current: queries.Scoring, job: dict, waived: Collection[str] = (), alone: bool = False
) -> str | None:
    """Why the Writer can't work on this job (a PROBLEMS key), or None: Prepare, Request changes and Write cover letter
    alike. `waived`: the doubts the pop-up asked you about, so the Writer proceeds anyway; `alone`: Resume only, so no
    council is needed."""
    if doubt := next((doubt for doubt in _doubts(conn, current, job) if doubt not in waived), None):
        return doubt
    if writer_sources.unconfirmed(
        writer_sources.offered(conn, current.resume), writer_sources.of_job(conn, job["id"])[0]
    ):
        return "tiles"
    if not _writer_chosen():
        return "writer"
    return None if alone or _council_chosen() else "council"


def _busy(conn: sqlcipher3.Connection, job_id: int) -> bool:
    """Whether a version of this job (resume or cover letter) is being written."""
    return runs.writing(conn, job_id)


def _doubts(conn: sqlcipher3.Connection, current: queries.Scoring, job: dict) -> list[str]:
    """The DOUBTS that hold for this job now."""
    found = {
        "skipped": job["not_interested"], "closed": job["status"] != "open",
        "not-ready": queries.job_scoring(conn, job["id"], current)["status"] != "ready",
    }  # fmt: skip
    return [doubt for doubt, holds in found.items() if holds]


def waived_of(request: Request) -> frozenset[str]:
    """The doubts the pop-up asked you about before this press (none from a page that didn't ask)."""
    return frozenset(request.state.form.get("anyway", "").split())


def _one_page() -> bool:
    try:
        return load_config().documents.one_page
    except ConfigError:  # shown on the Settings page; the default meanwhile
        return True


def _council_chosen() -> bool:
    try:
        return load_config().ai.council_chosen
    except ConfigError:
        return False


def _writer_chosen() -> bool:
    try:
        return load_config().ai.writer is not None
    except ConfigError:  # shown on the Settings page; nothing can be prepared meanwhile
        return False


def _file(conn: sqlcipher3.Connection, number: str) -> uploads.ResumeFile | None:
    """The uploaded file a page is about; None for your Resume Profile. An unknown or removed one: not found."""
    if not number:
        return None
    file = uploads.get(conn, int(number)) if number.isascii() and number.isdigit() and len(number) < 19 else None
    if file is None:
        raise HTTPException(404)
    return file


def _tiles_view(conn: sqlcipher3.Connection, data_dir: Path, file: uploads.ResumeFile | None = None) -> dict:
    """What the Tiles page shows for one source (your Resume Profile, or an uploaded file): its confirmed tiles if
    made from its current text, else a fresh reading."""
    files = uploads.offered(conn)
    if file:
        latex, resume = uploads.latex(conn, file.id), file.fingerprint
    else:
        try:
            latex = read_latex(data_dir)
            resume = from_latex(latex).fingerprint
        except ResumeError as error:
            return {"resume_problem": str(error), "rows": [], "kinds": PREFIXES, "files": files, "file": None}
    reading, confirmed = read_tiles(latex), tile_sets.latest(conn, file and file.id)
    current = confirmed is not None and confirmed.resume == resume
    tiles = confirmed.tiles if current else reading.tiles
    contact = questionnaire.load(conn)[0]["contact"]
    return {
        "resume": resume, "confirmed": confirmed, "current": current, "kinds": PREFIXES,
        "rows": [tile_sets.as_row(tile, position) for position, tile in enumerate(tiles, 1)],
        "changes": tile_sets.changes(confirmed.tiles, reading.tiles) if confirmed and not current else [],
        "unplaced": reading.unplaced, "reading_problem": reading.problem,
        "contact_empty": not any(contact.get(field) for field, _ in tile_sets.CONTACT),
        "contact_missing": tile_sets.contact_check(contact, header_text(latex)),
        "pdf": pdf.built(pdf.master(data_dir, confirmed.set_id)) if current and not file else None,
        "one_page": _one_page(), "files": files, "file": file, "default_sources": writer_sources.default(conn),
    }  # fmt: skip
