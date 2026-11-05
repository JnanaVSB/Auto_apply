"""The job box's Application form part (Phase 7): the job's form read from its site's public data (Greenhouse's job
board, Lever's application page), saved, and shown with what would be filled in, from where, and what is left for you.
You answer what is left here, for this job (form_answers, given by you) or for every form (your Questionnaire), and
Write answers asks the Writer for the long ones. The app only downloads a form's public questions and never sends
anything to a job site (owner rule: never a submission without your explicit authorization)."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import sqlcipher3
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from app import questionnaire, sources, submit, tracker
from app.collect import now
from app.config import ConfigError, load_config
from app.council import answers, flow, runs
from app.council.answers import KEPT
from app.fetch import FetchError
from app.resume import CONTROL
from app.submit import Field, FormError, greenhouse, lever, match, record
from app.web import queries

READERS = {"greenhouse": (greenhouse, "api"), "lever": (lever, "page")}  # site -> its reader, and where it reads from
READ_FROM = {"api": "Greenhouse's public job board", "page": "Lever's application page", "browser": "the browser"}
ASHBY = "Ashby forms are read in the browser: press Fill in browser (coming in the next part)"
NO_RESUME = "The form is read once the resume is prepared"
ALREADY = "Already reading this form"
TO_REMEMBER = ("text", "single", "multi")  # a required question no rule answers: kept in More answers to answer once
TYPED = ("text", "location", "long_text", "single", "multi")  # the questions you answer here
MAX_LINE, MAX_LONG = 2_000, 20_000  # characters of a short and a long answer
YOURS = """INSERT INTO form_answers (job_id, field, answer, given_by, preparation_id, saved_on, wording)
    VALUES (?, ?, ?, 'you', NULL, ?, ?) ON CONFLICT (job_id, field) DO UPDATE SET answer = excluded.answer,
    given_by = 'you', preparation_id = NULL, saved_on = excluded.saved_on, wording = excluded.wording"""
FORGET = "DELETE FROM form_answers WHERE job_id = ? AND field = ? AND given_by = 'you'"
GONE = "This question is no longer on the form: read it again"
NAMES_COMPANY = "This answer names {}, so it is kept for this job only"
NOT_HERE = "This question isn't answered here: do it in the window"
PROMISE = "You tick this yourself in the window: the app never answers it"
EMPTY = "Type an answer, or press Leave it empty"
MARKER = "That text is one the app keeps for itself: write your answer another way"
INVISIBLE = "The answer holds an invisible character: type it again"
NOT_A_CHOICE = "Choose from the form's own choices"
REQUIRED = "This form requires it: decide in the window, or skip this job"
NOT_OPTIONAL = "This question is required: answer it, or do it in the window"
NO_YES = "This consent has no single yes choice: decide in the window"
# worded as app/web/prepare.py's NO_WRITER and BUSY_JOB, which can't be imported here (prepare.py imports this module)
NO_WRITER = "Choose the Writer's model on the Writer settings page (Settings, then Writer settings) first."
BUSY_JOB = "The Writer is still working on this job: try again when it's done."
MINE, WRITER = "your answer for this job", "Writer draft"  # match's sources of a job's own answers


class _Refused(Exception):  # noqa: N818 (shown as it is)
    """An answer that can't be kept; the message says why, never with your text."""


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
    kept = {row[0]: row[1:] for row in conn.execute(KEPT, (job_id,))}
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
        first = first and record.recorded(conn, job_id) is None  # you applied: nothing more to read
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
            ordinary = match.care(line.field, job["company"]) == match.ORDINARY
            if (
                ordinary
                and line.decision == "unknown"
                and not line.note
                and line.field.required
                and _answerable(line.field)
            ):
                try:
                    match.remember(conn, line.field.label, job["company"])
                except questionnaire.QuestionnaireError as error:
                    return str(error)
        return None

    @app.post("/jobs/{job_id:int}/form/answer")
    async def answer(request: Request, job_id: int) -> Response:
        """Your answer to one question, for this job; with Save ticked (short questions), also in your More answers
        for every form."""
        conn, (job, field, refused) = request.app.state.conn, _asked(request, job_id)
        if refused:
            return _part(request, job, failed=refused)
        try:
            if field.kind not in TYPED:
                raise _Refused(NOT_HERE)
            text = _typed(field, request.state.form)
        except _Refused as refused:
            return _part(request, job, failed=str(refused))
        _keep(conn, job_id, field, text)
        saved = field.kind != "long_text" and match.care(field, job["company"]) == match.ORDINARY
        if saved and request.state.form.get("save") == "1":
            if match.names(text, job["company"]):
                return _part(request, job, note=NAMES_COMPANY.format(job["company"]))
            try:
                match.remember(conn, field.label, job["company"], text)
            except questionnaire.QuestionnaireError as error:  # the answer for this job is kept all the same
                return _part(request, job, note=str(error))
        return _part(request, job)

    @app.post("/jobs/{job_id:int}/form/consent")
    async def consent(request: Request, job_id: int) -> Response:
        """Accept or Decline: remembered for every form for a known kind (your Questionnaire's Consents), else for
        this job only. AI-use promises and arbitration are never answered by the app."""
        conn, (job, field, refused) = request.app.state.conn, _asked(request, job_id)
        if refused:
            return _part(request, job, failed=refused)
        choice = request.state.form.get("choice")
        kind = (match.consent_kind(field) or (None,))[0]
        if kind is None or choice not in ("accept", "decline"):
            return _part(request, job, failed=NOT_HERE)
        if kind and match.plain_consent(field):  # a sentence that goes further is for this job only
            values, _, problem = questionnaire.load(conn)
            if problem:
                return _part(request, job, failed=match.BROKEN.format(problem))
            values["consents"][kind] = choice
            questionnaire.save(conn, questionnaire.check(values))
        elif choice == "accept":
            if not (given := _yes(field)):
                return _part(request, job, failed=NO_YES)
            _keep(conn, job_id, field, given)
        elif field.required:
            return _part(request, job, failed=REQUIRED)
        else:
            _keep(conn, job_id, field, match.LEFT_EMPTY)
        return _part(request, job)

    @app.post("/jobs/{job_id:int}/form/window")
    async def in_window(request: Request, job_id: int) -> Response:
        """I'll do this in the window myself: for this job, the app leaves the question to you."""
        job, field, refused = _asked(request, job_id, promise=True)
        if refused:
            return _part(request, job, failed=refused)
        _keep(request.app.state.conn, job_id, field, match.IN_WINDOW)
        return _part(request, job)

    @app.post("/jobs/{job_id:int}/form/empty")
    async def left_empty(request: Request, job_id: int) -> Response:
        """Leave it empty, for this job: optional questions only."""
        job, field, refused = _asked(request, job_id)
        if refused or field.required:
            return _part(request, job, failed=refused or NOT_OPTIONAL)
        _keep(request.app.state.conn, job_id, field, match.LEFT_EMPTY)
        return _part(request, job)

    @app.post("/jobs/{job_id:int}/form/forget")
    async def forget(request: Request, job_id: int) -> Response:
        """Change: your answer for this job is forgotten, so the rules answer the question again."""
        job, field, refused = _asked(request, job_id, promise=True)
        if refused:
            return _part(request, job, failed=refused)
        with request.app.state.conn as conn:
            conn.execute(FORGET, (job_id, field.key))
        return _part(request, job)

    @app.post("/jobs/{job_id:int}/form/write")
    async def write(request: Request, job_id: int) -> Response:
        """Write answers: the Writer drafts the form's long questions, in the background."""
        conn, job = request.app.state.conn, _job(request, job_id)
        if not _writer_chosen():
            return _part(request, job, failed=NO_WRITER)
        if runs.writing(conn, job_id):
            return _part(request, job, failed=BUSY_JOB)
        try:
            flow.start(conn, load_config(), job_id, runs.ANSWERS)
        except answers.NoQuestions as refused:
            return _part(request, job, failed=str(refused))
        request.app.state.preparer.wake()
        return Response(headers={"HX-Refresh": "true"})  # the page again: the box above says so too

    @app.post("/jobs/{job_id:int}/form/submitted")
    async def submitted(request: Request, job_id: int) -> Response:
        """I submitted it: records what you did on the site and moves the card to Applied. The app sends nothing."""
        conn, job = request.app.state.conn, _job(request, job_id)
        found = preview(conn, data_dir, job_id)
        try:
            on = date.fromisoformat(request.state.form.get("on", ""))
        except ValueError:
            return _part(request, job, failed=tracker.BAD_DATE)
        try:
            record.submit(conn, data_dir, job, on, found.lines if found else None)
        except record.Refused as refused:
            return _part(request, job, failed=str(refused))
        return _part(request, job)

    @app.get("/jobs/{job_id:int}/form/writing")
    async def writing(request: Request, job_id: int) -> Response:
        """While the Writer writes the answers, the part's status line asks this every 3 s; once it is done the page
        reloads, so a box you are typing in is never redrawn under you."""
        latest = runs.latest(request.app.state.conn, _job(request, job_id)["id"], runs.ANSWERS)
        if latest and latest["status"] == "writing":
            return page(request, "form.html", writing_only=True, job_id=job_id)
        return Response(headers={"HX-Refresh": "true"})

    def _asked(request: Request, job_id: int, promise: bool = False) -> tuple[dict, Field | None, str | None]:
        """The job, the question a press is about (by its key on the saved form) and why it can't be answered (it
        is gone, or it is an AI-use promise or an arbitration agreement, unless `promise`), else None."""
        job, key = _job(request, job_id), request.state.form.get("field", "")
        saved = submit.load(request.app.state.conn, job_id)
        field = next((field for field in saved[0].fields if field.key == key), None) if saved else None
        if field is None or field.kind == "hidden":
            return job, None, GONE
        if not promise and match.care(field, job["company"]) == match.PROMISE:
            return job, field, PROMISE
        return job, field, None

    def _part(request: Request, job: dict, note: str | None = None, failed: str | None = None) -> HTMLResponse:
        conn, job_id = request.app.state.conn, job["id"]
        posting = sources.posting_of(job["apply_url"])
        site = posting and posting[0]
        found = preview(conn, data_dir, job_id)
        lines = found.lines if found else ()
        latest, done = runs.latest(conn, job_id, runs.ANSWERS), runs.last_done(conn, job_id, runs.ANSWERS)
        flags = (
            {flag["field"]: ", ".join(flag["numbers"]) for flag in json.loads(done["flags"] or "[]")} if done else {}
        )
        longs = [  # the long questions: the Writer's to draft, its drafts, or your own answers
            (line, _limit(answers.limit(line.field)), flags.get(line.field.key))
            for line in lines
            if line.field.kind == "long_text"
            and (line.status == "write" or (line.status == "fill" and line.source in (WRITER, MINE)))
        ]  # fmt: skip
        return page(
            request, "form.html", job_id=job_id, found=found, note=note, failed=failed,
            readable=site in READERS,
            waiting=NO_RESUME if site in READERS and not found and not runs.last_done(conn, job_id) else None,
            elsewhere=ASHBY if site == "ashby" else None,
            read_on=found and _day(found.read_on), read_from=found and READ_FROM[found.read_from],
            shown=[(line, record.shown(line)) for line in lines if line.status != "hidden"],
            hidden=sum(line.status == "hidden" for line in lines), decisions=match.DECISIONS,
            remembered={line.field.key for line in lines if line.decision == "consent"
                        and (match.consent_kind(line.field) or ("",))[0] and match.plain_consent(line.field)},
            askable={line.field.key for line in lines if line.askable and _answerable(line.field)},
            this_job_only={line.field.key for line in lines
                           if match.care(line.field, job["company"]) != match.ORDINARY},
            longs=longs, own_words=found is not None and match.NO_AI_WARNING in found.warnings,
            writing=latest is not None and latest["status"] == "writing",
            stopped=latest["problem"] if latest and latest["status"] in runs.FAILED else None, mine=MINE,
            job=job, record=record.recorded(conn, job_id), today=date.today().isoformat(),
            panel=record.standard(questionnaire.load(conn)[0]),
            papers=[(found_file[0], f"/documents/{job_id}/{found_file[1].name}")
                    for found_file in match.documents(conn, data_dir, job_id).values() if found_file],
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


def _typed(field: Field, form: dict[str, str]) -> str:
    """Your answer as it is kept: text as typed (one line for a short box), or the chosen choices' labels, one a line,
    so the rules' choice matching finds them; raises _Refused saying what is wrong."""
    if field.kind in ("single", "multi"):
        labels = {choice.value: choice.label for choice in field.choices}
        chosen = [form.get("answer", "")] if field.kind == "single" else [
            value for name, value in form.items() if name.startswith("choice.")
        ]  # fmt: skip
        if not chosen or not all(value in labels for value in chosen):
            raise _Refused(NOT_A_CHOICE)
        return "\n".join(dict.fromkeys(labels[value] for value in chosen))
    text = form.get("answer", "").replace("\r\n", "\n")  # a browser sends a long box's new lines as \r\n
    if CONTROL.search(text):
        raise _Refused(INVISIBLE)
    text = text.strip() if field.kind == "long_text" else " ".join(text.split())
    if not text:
        raise _Refused(EMPTY)
    if text in (match.IN_WINDOW, match.LEFT_EMPTY):
        raise _Refused(MARKER)
    most = MAX_LONG if field.kind == "long_text" else MAX_LINE
    if len(text) > most:
        raise _Refused(f"An answer here is at most {most:,} characters")
    if field.kind == "long_text" and answers.STATED.search(f"{field.label} {field.help}"):
        words, characters = answers.limit(field)
        if (words and len(text.split()) > words) or (characters and len(text) > characters):
            raise _Refused(f"This question allows at most {_limit((words, characters))}")
    return text


def _keep(conn: sqlcipher3.Connection, job_id: int, field: Field, answer: str) -> None:
    """Your answer for this job's question as it is worded now, replacing a Writer draft."""
    with conn:
        conn.execute(YOURS, (job_id, field.key, answer, now(), match.wording(field)))


def _yes(field: Field) -> str:
    """A consent given for this job: its box ticked, or the label of the choice that gives it ("" without one)."""
    if field.kind == "consent":
        return match.TICKED
    choice = match.consent_choice(field)
    return choice.label if choice and field.kind in ("single", "multi") else ""


def _limit(most: tuple[int | None, int | None]) -> str:
    """A question's limit in words: "100 words", "1,200 characters", or both."""
    return " and ".join(f"{count:,} {unit}" for count, unit in zip(most, ("words", "characters"), strict=True) if count)


def _writer_chosen() -> bool:
    """Whether the Writer has a model, as the Ready to Apply box checks it (app/web/prepare.py)."""
    try:
        return load_config().ai.writer is not None
    except ConfigError:  # shown on the Settings page
        return False


def _day(read_on: str) -> str:
    """When the form was read, on this computer's calendar: "5 Nov"."""
    day = datetime.fromisoformat(read_on).astimezone()
    return f"{day.day} {day:%b}"
