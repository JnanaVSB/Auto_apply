"""The job box's Application form part (Phase 7): the job's form read from its site's public data (Greenhouse's job
board, Lever's application page), saved, and shown with what would be filled in, from where, and what is left for you.
You answer what is left here, for this job (form_answers, given by you) or for every form (your Questionnaire), and
Write answers asks the Writer for the long ones. The app only downloads a form's public questions and never sends
anything to a job site (owner rule: never a submission without your explicit authorization)."""

import hashlib
import json
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

import sqlcipher3
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from app import questionnaire, sources, submit, tracker
from app.config import ConfigError, load_config
from app.council import answers, flow, runs
from app.resume import CONTROL
from app.submit import Field, FormError, fill_words, fills, greenhouse, lever, match, preview, record, wordings
from app.submit.match import MINE, WRITER
from app.submit.preview import Preview
from app.web import actions, queries
from app.web.prepare import BUSY_JOB, NO_WRITER, writer_chosen

READERS = {"greenhouse": (greenhouse, "api"), "lever": (lever, "page")}  # site -> its reader, and where it reads from
READ_FROM = {"api": "Greenhouse's public job board", "page": "Lever's application page", "browser": "the browser"}
ASHBY = "Ashby forms are read in the browser: press Read the form."
NOT_SEEN = (
    "The plugin can't read yes/no buttons or questions that appear after an answer: do those yourself on the form. "
    "These questions are what the page showed, and a script on the page can change them: read them before you fill."
)
NO_RESUME = "The form is read once the resume is prepared"
MAX_REMEMBERED = 25  # a page writes its own form, so one read of it can't fill More answers
SOME_REMEMBERED = "Only the first {} of this form's questions were kept in More answers: answer the rest here."
ALREADY = "Already reading this form"
TO_REMEMBER = ("text", "single", "multi")  # a required question no rule answers: kept in More answers to answer once
TYPED = ("text", "location", "long_text", "single", "multi")  # the questions you answer here
MAX_LINE, MAX_LONG = 2_000, 20_000  # characters of a short and a long answer
GONE = "This question is no longer on the form: read it again"
NAMES_COMPANY = "This answer names {}, so it is kept for this job only"
NOT_HERE = "This question isn't answered here: do it in the window"
PROMISE = "You do this yourself in the window: the app never answers it"
EMPTY = "Type an answer, or press Leave it empty"
MARKER = "That text is one the app keeps for itself: write your answer another way"
INVISIBLE = "The answer holds an invisible character: type it again"
NOT_A_CHOICE = "Choose from the form's own choices"
REQUIRED = "This form requires it: decide in the window, or skip this job"
NOT_OPTIONAL = "This question is required: answer it, or do it in the window"
NO_YES = "This consent has no single yes choice: decide in the window"
CHANGED_SUGGESTION = "This suggestion changed: look at it again"
CHANGED_QUESTION = "This question changed: look at it again"
SAME_DRAFT = "This is the Writer's draft unchanged: only your own changed words close it"
ON_PHONE = "Filling happens on a computer. Your answers are saved here."
FILLING = "Filling in the new tab… Reload this page to see how it went."
NOT_FILLED = "The form opened without filling."
READING = "Reading in the new tab… Reload this page when it's done."
NOT_READ = "The form opened without reading it."
PRESS_FAILED = "That press couldn't be saved."


class _Refused(Exception):  # noqa: N818 (shown as it is)
    """An answer that can't be kept; the message says why, never with your text."""


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
        except FormError as error:  # its messages never hold your details
            return _part(request, job, failed=str(error))
        finally:
            state.forms_reading.discard(job_id)
        submit.save(state.conn, job_id, form, read_from, unread=unread)
        return _part(request, job, note=remember(state.conn, data_dir, job))

    @app.post("/jobs/{job_id:int}/form/answer")
    async def answer(request: Request, job_id: int) -> Response:
        """Your answer to one question, for this job; with Save ticked (short questions), also in your More answers
        for every form."""
        conn, (job, field, refused) = request.app.state.conn, _asked(request, job_id)
        if refused or _stale(request, field):
            return _part(request, job, failed=refused or CHANGED_QUESTION)
        try:
            text = _typed(field, request.state.form)
        except _Refused as refused:
            return _part(request, job, failed=str(refused))
        if _same_draft(preview.preview(conn, data_dir, job_id), field, text):
            return _part(request, job, failed=SAME_DRAFT)
        preview.keep(conn, job_id, field, text)
        saved = field.kind != "long_text" and wordings.care(field, job["company"]) == wordings.ORDINARY
        if saved and request.state.form.get("save") == "1":
            if wordings.names(text, job["company"]):
                return _part(request, job, note=NAMES_COMPANY.format(job["company"]))
            try:
                questionnaire.remember(conn, field.label, job["company"], text)
            except questionnaire.QuestionnaireError as error:  # the answer for this job is kept all the same
                return _part(request, job, note=str(error))
        return _part(request, job)

    @app.post("/jobs/{job_id:int}/form/consent")
    async def consent(request: Request, job_id: int) -> Response:
        """Accept or Decline: remembered for every form for a known kind (your Questionnaire's Consents), else for
        this job only. AI-use promises and arbitration are never answered by the app."""
        conn, (job, field, refused) = request.app.state.conn, _asked(request, job_id)
        if refused or _stale(request, field):
            return _part(request, job, failed=refused or CHANGED_QUESTION)
        choice = request.state.form.get("choice")
        kind = wordings.consent_kind(field, job["company"])
        if kind is None or choice not in ("accept", "decline"):
            return _part(request, job, failed=NOT_HERE)
        if kind:  # any other sentence is for this job only
            values, _, problem = questionnaire.load(conn)
            if problem:
                return _part(request, job, failed=questionnaire.BROKEN.format(problem))
            values["consents"][kind] = choice
            questionnaire.save(conn, questionnaire.check(values))
        elif choice == "accept":
            if not (given := _yes(field)):
                return _part(request, job, failed=NO_YES)
            preview.keep(conn, job_id, field, given)
        elif field.required:
            return _part(request, job, failed=REQUIRED)
        else:
            preview.keep(conn, job_id, field, match.LEFT_EMPTY)
        return _part(request, job)

    @app.post("/jobs/{job_id:int}/form/use")
    async def use(request: Request, job_id: int) -> Response:
        """Use: a suggested answer becomes your answer for this job, if it is still the one the page showed."""
        conn, (job, field, refused) = request.app.state.conn, _asked(request, job_id)
        if refused:
            return _part(request, job, failed=refused)
        line = next(
            (line for line in _usable(preview.preview(conn, data_dir, job_id)) if line.field.key == field.key), None
        )
        if line is None or request.state.form.get("seen") != _seen([line]):
            return _part(request, job, failed=CHANGED_SUGGESTION)
        preview.keep(conn, job_id, field, _suggestion(line))
        return _part(request, job)

    @app.post("/jobs/{job_id:int}/form/use-all")
    async def use_all(request: Request, job_id: int) -> Response:
        """Use all: every suggested answer becomes your answer for this job, if the list is still the one shown."""
        conn, job = request.app.state.conn, _job(request, job_id)
        lines = _usable(preview.preview(conn, data_dir, job_id))
        if request.state.form.get("seen") != _seen(lines):
            return _part(request, job, failed=CHANGED_SUGGESTION)
        for line in lines:
            preview.keep(conn, job_id, line.field, _suggestion(line))
        return _part(request, job)

    @app.post("/jobs/{job_id:int}/form/window")
    async def in_window(request: Request, job_id: int) -> Response:
        """I'll do this in the window myself: for this job, the app leaves the question to you."""
        job, field, refused = _asked(request, job_id, promise=True)
        if refused:
            return _part(request, job, failed=refused)
        preview.keep(request.app.state.conn, job_id, field, match.IN_WINDOW)
        return _part(request, job)

    @app.post("/jobs/{job_id:int}/form/empty")
    async def left_empty(request: Request, job_id: int) -> Response:
        """Leave it empty, for this job: optional questions only."""
        job, field, refused = _asked(request, job_id)
        if refused or field.required:
            return _part(request, job, failed=refused or NOT_OPTIONAL)
        preview.keep(request.app.state.conn, job_id, field, match.LEFT_EMPTY)
        return _part(request, job)

    @app.post("/jobs/{job_id:int}/form/forget")
    async def forget(request: Request, job_id: int) -> Response:
        """Change: your answer for this job is forgotten, so the rules answer the question again."""
        job, field, refused = _asked(request, job_id, promise=True)
        if refused:
            return _part(request, job, failed=refused)
        preview.forget(request.app.state.conn, job_id, field.key)
        return _part(request, job)

    @app.post("/jobs/{job_id:int}/form/write")
    async def write(request: Request, job_id: int) -> Response:
        """Write answers: the Writer drafts the form's long questions, in the background."""
        conn, job = request.app.state.conn, _job(request, job_id)
        if not writer_chosen():
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
        found = preview.preview(conn, data_dir, job_id)
        try:
            on = date.fromisoformat(request.state.form.get("on", ""))
        except ValueError:
            return _part(request, job, failed=tracker.BAD_DATE)
        try:
            note = record.submit(conn, data_dir, job, on, found.lines if found else None)
        except record.Refused as refused:
            return _part(request, job, failed=str(refused))
        with conn:
            request.app.state.tickets.end_job(conn, job_id)
        return _part(request, job, note=note)

    @app.post("/jobs/{job_id:int}/form/unsent")
    async def unsent(request: Request, job_id: int) -> Response:
        """I didn't send it: closes the fill and wipes its read. Nothing is recorded and no card moves."""
        conn, job = request.app.state.conn, _job(request, job_id)
        if not record.recorded(conn, job_id):
            try:
                request.app.state.tickets.not_sent(conn, job_id)
            except sqlcipher3.OperationalError as error:
                return _part(request, job, failed=actions.BUSY if "locked" in str(error) else PRESS_FAILED)
        return _part(request, job)

    @app.post("/jobs/{job_id:int}/form/computer")
    async def for_computer(request: Request, job_id: int) -> Response:
        """Do this on a computer, or Take it off: from either address, so the laptop can take a job off too."""
        value = request.state.form.get("value") == "1"
        if not actions.set_for_computer(request.app.state.conn, queries.known(job_id), value):
            raise HTTPException(404)
        if request.state.form.get("to") == "ready":  # Ready's own list
            return RedirectResponse("/ready", 303)
        return _part(request, _job(request, job_id))

    @app.post("/jobs/{job_id:int}/form/fill")
    async def fill(request: Request, job_id: int) -> Response:
        """Open and fill: the press also opens the form in a new tab, so whatever happens here, it answers with only the
        line beside the button (hx-swap="none" still takes it, where a refusal would show nothing)."""
        return page(request, "form.html", fill_status=_pressed(request, _job(request, job_id)))

    @app.post("/jobs/{job_id:int}/form/read-in-browser")
    async def read_in_browser(request: Request, job_id: int) -> Response:
        """Read the form, for a form only a browser can see: like Open and fill, it answers with only the line beside
        the button."""
        return page(request, "form.html", fill_status=_read_pressed(request, _job(request, job_id)))

    def _read_pressed(request: Request, job: dict) -> str:
        if request.state.remote:
            return ON_PHONE
        try:
            reason = request.app.state.tickets.make_read(request.app.state.conn, job)
        except sqlcipher3.OperationalError as error:
            reason = actions.BUSY if "locked" in str(error) else PRESS_FAILED
        return READING if reason is None else f"{reason} {NOT_READ}".strip()

    def _pressed(request: Request, job: dict) -> str:
        """What came of a press, once the fill is made or refused."""
        if request.state.remote:
            return ON_PHONE
        conn = request.app.state.conn
        try:
            limits = load_config()
            reason = request.app.state.tickets.make(conn, job, preview.preview(conn, data_dir, job["id"]), limits)
        except ConfigError as error:
            reason = str(error)
        except sqlcipher3.OperationalError as error:
            reason = actions.BUSY if "locked" in str(error) else PRESS_FAILED
        return FILLING if reason is None else f"{reason} {NOT_FILLED}".strip()

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
        if not promise and wordings.care(field, job["company"]) == wordings.PROMISE:
            return job, field, PROMISE
        return job, field, None

    def _part(request: Request, job: dict, note: str | None = None, failed: str | None = None) -> HTMLResponse:
        conn, job_id = request.app.state.conn, job["id"]
        posting = sources.posting_of(job["apply_url"])
        site = posting and posting[0]
        found = preview.preview(conn, data_dir, job_id)
        lines = found.lines if found else ()
        latest, done = runs.latest(conn, job_id, runs.ANSWERS), runs.last_done(conn, job_id, runs.ANSWERS)
        recorded = record.recorded(conn, job_id)
        back = recorded and record.moved_back(conn, job_id, recorded)
        flags = (
            {flag["field"]: ", ".join(flag["numbers"]) for flag in json.loads(done["flags"] or "[]")} if done else {}
        )
        suggested = _usable(found)
        fill_address, fill_reason = _fill_button(request, job, found)
        read_address, read_reason = _read_button(request, job)
        no_resume = site in (*READERS, "ashby") and not found and not runs.last_done(conn, job_id)
        longs = [  # the long questions: the Writer's to draft, its drafts, or your own answers
            (line, answers.in_words(answers.limit(line.field), ","), flags.get(line.field.key), _over_limit(line))
            for line in lines
            if line.field.kind == "long_text"
            and (line.status == "write" or line.source == WRITER or (line.status == "fill" and line.source == MINE))
        ]  # fmt: skip
        return page(
            request, "form.html", job_id=job_id, found=found, note=note, failed=failed,
            readable=site in READERS,
            waiting=NO_RESUME if no_resume else None,
            elsewhere=ASHBY if site == "ashby" and not no_resume and fills.form_address(job) else None,
            not_seen=NOT_SEEN if found and found.read_from == "browser" else None,
            read_on=found and _day(found.read_on), read_from=found and READ_FROM[found.read_from],
            shown=[(line, record.shown(line)) for line in lines if line.status != "hidden"],
            hidden=sum(line.status == "hidden" for line in lines), decisions=match.DECISIONS,
            remembered={line.field.key for line in lines if line.decision == "consent"
                        and wordings.consent_kind(line.field, job["company"])},
            askable={line.field.key for line in lines
                     if line.askable and (line.field.kind == "long_text" or _answerable(line.field))},
            this_job_only={line.field.key for line in lines
                           if wordings.care(line.field, job["company"]) != wordings.ORDINARY},
            seen={line.field.key: _seen([line]) for line in suggested}, seen_all=_seen(suggested),
            worded={line.field.key: _fingerprint(wordings.wording(line.field)) for line in lines},
            longs=longs, own_words=found is not None and found.no_writer,
            writing=latest is not None and latest["status"] == "writing",
            stopped=latest["problem"] if latest and latest["status"] in runs.FAILED else None, mine=MINE, writer=WRITER,
            max_line=MAX_LINE, max_long=MAX_LONG,
            fill_address=fill_address, fill_reason=fill_reason, last_fill=fill_words.latest(conn, job_id),
            read_address=read_address, read_reason=read_reason,
            job=job, record=recorded, heading=recorded and record.heading(recorded),
            markable=request.state.remote and queries.listable(conn, job_id),
            caution=recorded and record.caution(recorded), no_copy=record.NO_COPY, another_file=record.ANOTHER_FILE,
            moved_back=back and record.MOVED_BACK.format(_day(back)),
            today=date.today().isoformat(),
            panel=record.standard(questionnaire.load(conn)[0]),
            papers=[(found_file[0], f"/documents/{job_id}/{found_file[1].name}")
                    for found_file in record.documents(conn, data_dir, job_id).values() if found_file],
        )  # fmt: skip


def _fill_button(request: Request, job: dict, found: Preview | None) -> tuple[str | None, str]:
    """The form's address for the button, or what the box says in its place ("": nothing)."""
    try:
        reason = fills.why_not(request.app.state.conn, job, found, load_config())
    except ConfigError as error:
        reason = str(error)
    if request.state.remote and reason != "":
        return None, ON_PHONE
    return (fills.form_address(job), "") if reason is None else (None, reason)


def _read_button(request: Request, job: dict) -> tuple[str | None, str]:
    """The form's address for Read the form, or what the box says in its place ("": nothing)."""
    reason = fills.why_not_read(request.app.state.conn, job)
    if request.state.remote and reason != "":
        return None, ON_PHONE
    return (fills.form_address(job), "") if reason is None else (None, reason)


def remember(conn: sqlcipher3.Connection, data_dir: Path, job: dict, most: int | None = None) -> str | None:
    """Each required question no rule answers kept in your More answers as "needs answer", so you answer it once for
    every form, up to `most`; why it could not be (`most` reached, More answers full, or broken saved answers), else
    None."""
    kept = 0
    for line in preview.preview(conn, data_dir, job["id"]).lines:
        ordinary = wordings.care(line.field, job["company"]) == wordings.ORDINARY
        if (
            ordinary
            and line.decision == "unknown"
            and not line.note
            and line.field.required
            and _answerable(line.field)
        ):
            if kept == most:
                return SOME_REMEMBERED.format(most)
            try:
                questionnaire.remember(conn, line.field.label, job["company"])
            except questionnaire.QuestionnaireError as error:
                return str(error)
            kept += 1
    return None


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
    if field.kind not in TYPED:
        raise _Refused(NOT_HERE)
    if field.kind in ("single", "multi"):
        labels = field.labels
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
    return text


def _suggestion(line: match.Line) -> str:
    """A suggested answer as `_typed` keeps yours: its text, the labels of its choices, one a line, or a file's label,
    never its path."""
    if line.field.kind == "file":
        return line.source
    if isinstance(line.value, str):
        return line.value
    return "\n".join(line.field.labels[value] for value in line.value)


def _usable(found: Preview | None) -> list[match.Line]:
    """The suggestions Use may keep: never a Writer draft on a form with a rule about AI: only your edit closes it."""
    if found is None:
        return []
    return [
        line for line in found.lines if line.status == "suggested" and not (found.no_writer and line.source == WRITER)
    ]


def _fingerprint(shown: object) -> str:
    """What a page showed, for the press that comes back from it. Not the text: a browser changes the new lines of a
    value it sends back."""
    return hashlib.sha256(json.dumps(shown).encode()).hexdigest()


def _seen(lines: list[match.Line]) -> str:
    """Suggestions as the page shows them: Use checks this, so a saved answer never goes unseen."""
    return _fingerprint([(line.field.key, wordings.wording(line.field), record.shown(line)) for line in lines])


def _same_draft(found: Preview, field: Field, text: str) -> bool:
    """Whether this is the Writer's draft, untouched, on a form with a rule about AI."""
    line = next(line for line in found.lines if line.field.key == field.key)
    return found.no_writer and line.source == WRITER and text.split() == line.value.split()


def _stale(request: Request, field: Field) -> bool:
    """Whether the page was showing other words for this question: your answer was to those."""
    return request.state.form.get("wording") != _fingerprint(wordings.wording(field))


def _over_limit(line: match.Line) -> str:
    """The note on your own long answer over a limit the form states: it is yours to shorten, or to keep."""
    most = answers.stated(line.field)
    if not most or line.source != MINE or line.status != "fill":
        return ""
    words, characters = most
    over = (words and len(line.value.split()) > words) or (characters and len(line.value) > characters)
    return f"over the form's stated limit of {answers.in_words(most, ',')}" if over else ""


def _yes(field: Field) -> str:
    """A consent given for this job: its box ticked, or the label of the choice that gives it ("" without one)."""
    if field.kind == "consent":
        return match.TICKED
    choice = wordings.consent_choice(field)
    return choice.label if choice and field.kind in ("single", "multi") else ""


def _day(read_on: str) -> str:
    """When the form was read, on this computer's calendar: "5 Nov"."""
    day = datetime.fromisoformat(read_on).astimezone()
    return f"{day.day} {day:%b}"
