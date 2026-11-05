"""The job box's Application form part (Phase 7): the job's form read from its site's public data (Greenhouse's job
board, Lever's application page), saved, and shown with what would be filled in, from where, and what is left for you.
Read-only: the app only downloads a form's public questions and never sends anything to a job site (owner rule: never
a submission without your explicit authorization)."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import sqlcipher3
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from app import questionnaire, sources, submit
from app.council import runs
from app.council.answers import KEPT
from app.fetch import FetchError
from app.submit import Field, FormError, greenhouse, lever, match
from app.web import queries

READERS = {"greenhouse": (greenhouse, "api"), "lever": (lever, "page")}  # site -> its reader, and where it reads from
READ_FROM = {"api": "Greenhouse's public job board", "page": "Lever's application page", "browser": "the browser"}
ASHBY = "Ashby forms are read in the browser: press Fill in browser (coming in the next part)"
OTHER = "This site's form isn't read by the app: the copy panel for it comes soon"
NO_RESUME = "The form is read once the resume is prepared"
ALREADY = "Already reading this form"
TO_REMEMBER = ("text", "single", "multi")  # a required question no rule answers: kept in More answers to answer once


@dataclass(frozen=True)
class Preview:
    """The job's saved form, matched to your answers: what the part and the Ready page's badge show."""

    read_on: str
    read_from: str
    problem: str | None  # questions that could not be read
    lines: tuple[match.Line, ...]
    warnings: tuple[str, ...]
    open: int  # decisions required


def preview(conn: sqlcipher3.Connection, data_dir: Path, job_id: int) -> Preview | None:
    """The job's form as it would be filled in now; None before it is read."""
    saved = submit.load(conn, job_id)
    if saved is None:
        return None
    form, read_on, read_from, problem = saved
    answers = questionnaire.load(conn)[0]
    company = conn.execute("SELECT company FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
    kept = {field: (answer, given_by) for field, answer, given_by in conn.execute(KEPT, (job_id,))}
    lines, warnings = match.match(
        form, answers, company, kept=kept, consents=answers.get("consents") or {},
        files=match.documents(conn, data_dir, job_id),
    )  # fmt: skip
    return Preview(read_on, read_from, problem, lines, warnings, sum(line.status == "decide" for line in lines))


def add_routes(app: FastAPI, page: Callable[..., HTMLResponse], data_dir: Path) -> None:
    app.state.forms_tried, app.state.forms_reading = set(), set()  # jobs whose form this dashboard read, or reads now

    @app.get("/jobs/{job_id:int}/form")
    async def form_part(request: Request, job_id: int) -> Response:
        """The part, loaded by the job page; the form is read by itself once, when the resume is ready."""
        conn, job = request.app.state.conn, _job(request, job_id)
        posting = sources.posting_of(job["apply_url"])
        first = job_id not in request.app.state.forms_tried and submit.load(conn, job_id) is None
        if posting and posting[0] in READERS and first and runs.last_done(conn, job_id):
            return await _read(request, job, posting)
        return _part(request, job)

    @app.post("/jobs/{job_id:int}/form/read")
    async def read_form(request: Request, job_id: int) -> Response:
        """Read the form (or Read again): its public questions downloaded from the site, and saved."""
        job = _job(request, job_id)
        posting = sources.posting_of(job["apply_url"])
        if posting and posting[0] in READERS:
            return await _read(request, job, posting)
        return _part(request, job)

    async def _read(request: Request, job: dict, posting: tuple[str, str, str]) -> Response:
        """Download beside the dashboard (sites can take a while); saved here, where the database lives. One read at
        a time per job; nothing is saved when the site fails."""
        state, job_id = request.app.state, job["id"]
        if job_id in state.forms_reading:
            return _part(request, job, note=ALREADY)
        reader, read_from = READERS[posting[0]]
        state.forms_reading.add(job_id)
        state.forms_tried.add(job_id)
        try:
            form, unread = await run_in_threadpool(reader.read, state.collector.fetcher, posting[1], posting[2])
        except (FormError, FetchError) as error:  # their messages never hold your details
            return _part(request, job, failed=str(error))
        finally:
            state.forms_reading.discard(job_id)
        submit.save(state.conn, job_id, form, read_from, unread=unread)
        return _part(request, job, note=_remember(request, job))

    def _remember(request: Request, job: dict) -> str | None:
        """Each required question no rule answers kept in your More answers as "needs answer", so you answer it once
        for every form; why it could not be (More answers full, or broken saved answers), else None."""
        conn = request.app.state.conn
        for line in preview(conn, data_dir, job["id"]).lines:
            if line.decision == "unknown" and not line.note and line.field.required and _answerable(line.field):
                try:
                    match.remember(conn, line.field.label, job["company"])
                except questionnaire.QuestionnaireError as error:
                    return str(error)
        return None

    def _part(request: Request, job: dict, note: str | None = None, failed: str | None = None) -> HTMLResponse:
        conn, job_id = request.app.state.conn, job["id"]
        posting = sources.posting_of(job["apply_url"])
        site = posting and posting[0]
        found = preview(conn, data_dir, job_id)
        lines = found.lines if found else ()
        return page(
            request, "form.html", job_id=job_id, found=found, note=note, failed=failed,
            readable=site in READERS,
            waiting=NO_RESUME if site in READERS and not found and not runs.last_done(conn, job_id) else None,
            elsewhere=ASHBY if site == "ashby" else None if site in READERS else OTHER,
            read_on=found and _day(found.read_on), read_from=found and READ_FROM[found.read_from],
            shown=[(line, _shown(line)) for line in lines if line.status != "hidden"],
            hidden=sum(line.status == "hidden" for line in lines), decisions=match.DECISIONS,
        )  # fmt: skip


def _job(request: Request, job_id: int) -> dict:
    job = queries.job(request.app.state.conn, queries.known(job_id))
    if job is None:
        raise HTTPException(404)
    return job


def _answerable(field: Field) -> bool:
    """A question a saved answer could answer: typed, or with its choices listed (Greenhouse's school, degree and
    discipline come without theirs)."""
    return field.kind in TO_REMEMBER and (field.kind == "text" or bool(field.choices))


def _shown(line: match.Line) -> str:
    """A line's value as you'd read it: a choice's label (never its value), an upload's version (never its path)."""
    field = line.field
    if line.status != "fill":
        return ""
    if field.kind == "file":
        return line.source
    if field.kind == "consent" and line.value == match.TICKED:
        return "ticked"
    if isinstance(line.value, tuple):
        labels = {choice.value: choice.label for choice in field.choices}
        return ", ".join(labels.get(value, value) for value in line.value)
    return line.value


def _day(read_on: str) -> str:
    """When the form was read, on this computer's calendar: "5 Nov"."""
    day = datetime.fromisoformat(read_on).astimezone()
    return f"{day.day} {day:%b}"
