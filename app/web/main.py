"""The dashboard web app: safety checks on every request, login, and the pages."""

import asyncio
import logging
import mimetypes
import re
from collections.abc import Callable
from contextlib import asynccontextmanager, closing
from functools import partial
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urlsplit

import sqlcipher3
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from app import db
from app.config import ConfigError, ScoringSettings, load_config
from app.fetch import Fetcher
from app.llm import ollama
from app.resume import ResumeError, read_resume
from app.web import (
    actions,
    answer_key,
    council_settings,
    council_view,
    drafts,
    pilot,
    prepare,
    queries,
    security,
    writer_settings,
)
from app.web.collector import Collector
from app.web.preparer import Preparer
from app.web.scorer import Scorer

HERE = Path(__file__).parent
LOCAL_HOSTS = {"127.0.0.1", "localhost"}  # the dashboard listens on this computer only
MAX_FORM_BYTES = 64 * 1024
# Uploaded resumes are sent as form text (app/web/static/upload.js): up to 5 files of 1 MB, which form encoding can
# make up to three times longer. Only this one address accepts that much.
UPLOAD = "/resume-profile/upload"
MAX_UPLOAD_BYTES = 16 * 1024 * 1024
DIGITS = re.compile(r"[0-9]+")  # plain digits only: Python's isdigit() also accepts "²"
EXPIRED = "This page has expired; reload it and try again."
BUSY = "The database is busy (a collection is saving). Try again in a moment."
BUSY_WAIT_MS = 1000  # a dashboard change waits at most this long for a collection's save, then says "busy"
log = logging.getLogger("app.web")
SCHEDULE_CHECK_SECONDS = 60
templates = Jinja2Templates(directory=HERE / "templates")  # escapes every value shown in a page
ANSWERS = ("yes", "maybe", "no", "clear")
LOW_SCORE = "/?f=1&status=open&scoring=low&scoring=quick&sort=score"  # the Low score tab: the list, so filtered
SCORE_THESE = 500  # jobs per press of "Score these", so a mis-click cannot queue every job
VIEWS = ("table", "cards")  # the job lists' two views; Cards until one is chosen
VIEW_COOKIE = "auto_apply_view"  # named for this app, like the session cookie: cookies on 127.0.0.1 are shared
LISTS = ("/", "/ready")  # the pages with the Table | Cards switch
LOOK_COOKIE = "auto_apply_look"  # this browser's colours and mode, "<colours>.<mode>", e.g. "E.dark"
SCHEMES = {  # the stylesheet's colour schemes (data-palette); the first is the default and needs no attribute
    "today": "Teal (default)",
    "A": "A · Olive and walnut",
    "B": "B · Sage and espresso",
    "C": "C · Moss and cream",
    "D": "D · Mix (A, warmer brown titles)",
    "E": "E · Forest rail and cognac",
    "F": "F · Espresso rail and olive",
    "G": "G · Olive-black and brass",
}
MODES = {"auto": "Auto (as this device)", "light": "Light", "dark": "Dark"}  # data-theme; the first is the default
templates.env.globals |= {
    "US_LABELS": queries.US_LABELS,
    "SCORING_LABELS": queries.SCORING_LABELS,
    "LOW_SCORE": LOW_SCORE,
    "SCORE_THESE": SCORE_THESE,
    "SOURCE_LABELS": queries.SOURCE_LABELS,  # the filter form's words, shared with its "Filters" summary
    "SORT_LABELS": queries.SORT_LABELS,
    "HIDDEN_LABELS": queries.HIDDEN_LABELS,
    "SCHEMES": SCHEMES,  # the Look form on Settings
    "MODES": MODES,
}
# Python has no type of its own for .woff2 and takes it from the computer, where it can be missing (sent as text)
mimetypes.add_type("font/woff2", ".woff2")


def create_app(
    data_dir: Path, logins: security.Logins | None = None, new_fetcher: Callable[[], Fetcher] = Fetcher
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with closing(db.connect(data_dir)) as conn:  # opened here, so pages use it from the server's own thread
            conn.execute(f"PRAGMA busy_timeout = {BUSY_WAIT_MS}")
            app.state.conn = conn
            schedule = asyncio.create_task(_schedule(app))
            app.state.preparer.wake()  # carries on with whatever an earlier dashboard or the terminal left
            try:
                yield
            finally:  # the dashboard is closing: so are the schedule, any collection and any scoring
                schedule.cancel()
                await asyncio.to_thread(app.state.collector.stop)
                await asyncio.to_thread(app.state.scorer.stop)
                app.state.collector.fetcher.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.logins = logins or security.Logins()
    app.state.collector = Collector(data_dir, new_fetcher())  # one downloader for everything the dashboard reads
    app.state.scorer = Scorer(data_dir, app.state.collector.fetcher)
    app.state.preparer = Preparer(data_dir)  # the council's runner
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.exception_handler(HTTPException)
    async def refused(request: Request, error: HTTPException) -> Response:
        return _error(request, error.status_code, error.detail)

    @app.middleware("http")
    async def guard(request: Request, call_next) -> Response:
        """The address check, login, anti-forgery check and safety headers, for every request."""
        try:
            response = await _checked(request, call_next)
        except HTTPException as refusal:
            response = _error(request, refusal.status_code, refusal.detail)
        except Exception as error:  # never a crash report in the browser, nor job data in the terminal
            if isinstance(error, sqlcipher3.OperationalError) and "locked" in str(error):
                response = _error(request, 503, BUSY)  # a collection is saving; waited BUSY_WAIT_MS already
            else:
                log.error("Unexpected error: %s", type(error).__name__)
                response = _error(
                    request,
                    500,
                    f"Something went wrong ({type(error).__name__}); nothing was saved. "
                    "Try again; if it keeps happening, restart the dashboard.",
                )
        response.headers.update(security.SAFETY_HEADERS)
        # public font files: kept a day, not re-sent per page
        if (
            request.url.path.startswith("/static/")
            and request.url.path.endswith(".woff2")
            and response.status_code in (200, 304)
        ):
            response.headers["Cache-Control"] = "private, max-age=86400"
        return response

    @app.get("/login")
    async def login_page(request: Request) -> Response:
        return _page(request, "login.html")

    @app.post("/login")
    async def log_in(request: Request) -> Response:
        logins: security.Logins = request.app.state.logins
        if not logins.locked_minutes():
            row = request.app.state.conn.execute("SELECT password_hash FROM dashboard_login").fetchone()
            if row is None:
                raise HTTPException(503, "No dashboard password is set. Run: python -m app.web reset-password")
            # the slow password check runs beside the server's own thread, so other pages keep answering
            token = await run_in_threadpool(logins.log_in, request.state.form.get("password", ""), row[0])
            if token:
                response = RedirectResponse("/", 303)
                response.set_cookie(security.COOKIE, token, httponly=True, samesite="strict", path="/")
                return response
        minutes = logins.locked_minutes()
        error = f"Too many wrong passwords. Try again in {minutes} minute(s)." if minutes else "Wrong password."
        return _page(request, "login.html", 429 if minutes else 401, error=error)

    @app.post("/view")
    async def choose_view(request: Request) -> Response:
        """Table or Cards for the job lists, kept in this browser; back to the same list, shown that way."""
        choice = request.state.form.get("view")
        if choice not in VIEWS:
            raise HTTPException(400, "Choose Table or Cards.")
        response = RedirectResponse(_in_view(request.state.form.get("back", ""), choice), 303)
        response.set_cookie(VIEW_COOKIE, choice, max_age=365 * 24 * 60 * 60, path="/", samesite="lax", httponly=True)
        return response

    @app.post("/look")
    async def choose_look(request: Request) -> Response:
        """Colours and mode for every page, kept in this browser; back to the Look section on Settings."""
        scheme, mode = request.state.form.get("scheme"), request.state.form.get("mode")
        if scheme not in SCHEMES or mode not in MODES:
            raise HTTPException(400, "Choose colours and a mode from the lists.")
        response = RedirectResponse("/settings?look=changed#look", 303)
        response.set_cookie(
            LOOK_COOKIE, f"{scheme}.{mode}", max_age=365 * 24 * 60 * 60, path="/", samesite="lax", httponly=True
        )
        return response

    @app.post("/logout")
    async def log_out(request: Request) -> Response:
        request.app.state.logins.log_out(request.cookies.get(security.COOKIE))
        response = RedirectResponse("/login", 303)
        response.delete_cookie(security.COOKIE, path="/")
        return response

    @app.get("/")
    async def jobs_list(request: Request) -> Response:
        conn, view, scoring = request.app.state.conn, queries.Filters.from_address(request.url.query), _scoring()
        page, choices = queries.list_jobs(conn, view, scoring), queries.choices(conn)
        low = bool(view.scoring) and set(view.scoring) <= {"low", "quick"}  # the Low score tab shows the reasons
        return _page(
            request, "jobs.html", view=view, page=page, choices=choices, options=queries.OPTIONS, low=low,
            counts=queries.counts(conn, scoring),
        )  # fmt: skip

    @app.get("/jobs/{job_id:int}")
    async def job_page(request: Request, job_id: int) -> Response:
        conn = request.app.state.conn
        job = queries.job(conn, queries.known(job_id))
        if job is None:
            raise HTTPException(404)
        job["scoring"] = queries.job_scoring(conn, job_id, _scoring())
        return _page(request, "job.html", job=job, back=_back(request.query_params.get("back", "")))

    @app.post("/jobs/{job_id:int}/prepare")
    async def prepare_anyway(request: Request, job_id: int) -> Response:
        form = request.state.form
        if not actions.set_prepare_anyway(request.app.state.conn, queries.known(job_id), form.get("value") == "1"):
            raise HTTPException(404)
        if form.get("to") == "list":  # pressed in the Low score list: back to that list
            return RedirectResponse(_back(form.get("back", "")), 303)
        return _to_job(job_id, form)

    @app.post("/jobs/{job_id:int}/score")
    async def score_job(request: Request, job_id: int) -> Response:
        if queries.job(request.app.state.conn, queries.known(job_id)) is None:
            raise HTTPException(404)
        request.app.state.scorer.notice = request.app.state.scorer.start([job_id])
        return RedirectResponse("/collect#scoring", 303)

    @app.post("/score")
    async def score_now(request: Request) -> Response:
        request.app.state.scorer.notice = request.app.state.scorer.start()
        return RedirectResponse("/collect#scoring", 303)  # so reloading the page never starts another run

    @app.post("/score/these")
    async def score_these(request: Request) -> Response:
        view = queries.Filters.from_address(urlsplit(_back(request.state.form.get("back", ""))).query)
        ids = queries.job_ids(request.app.state.conn, view, _scoring(), SCORE_THESE)
        request.app.state.scorer.notice = request.app.state.scorer.start(ids) if ids else "No jobs to score."
        return RedirectResponse("/collect#scoring", 303)

    @app.post("/score/stop")
    async def stop_scoring(request: Request) -> Response:
        request.app.state.scorer.cancel()
        return RedirectResponse("/collect#scoring", 303)

    @app.get("/score/progress")
    async def score_progress(request: Request) -> Response:
        return _page(request, "score_progress.html", scorer=request.app.state.scorer)

    @app.get("/answer-key")
    async def answer_key_page(request: Request) -> Response:
        conn, scoring = request.app.state.conn, _scoring()
        try:
            settings = load_config().scoring
        except ConfigError:  # shown on the Settings page; the defaults meanwhile
            settings = ScoringSettings()
        return _page(
            request, "answer_key.html", marks=answer_key.marks(conn), suggestions=answer_key.suggestions(conn),
            ai=answer_key.ai_agreement(conn, scoring.resume), quick=answer_key.quick_agreement(conn, settings),
            routes=answer_key.pass_through(conn, scoring), scoring=scoring, settings=settings,
            models=await _installed(), labels=answer_key.LABELS, thresholds=answer_key.AI_THRESHOLDS,
        )  # fmt: skip

    @app.post("/answer-key/score")
    async def score_marked(request: Request) -> Response:
        model = request.state.form.get("model", "")
        if model not in await _installed():  # only a model Ollama on this computer has downloaded
            raise HTTPException(400, "Choose a model that is downloaded in Ollama.")
        ids, scorer = answer_key.marked_open(request.app.state.conn), request.app.state.scorer
        scorer.notice = scorer.start(ids, model) if ids else "No marked open jobs to score: mark some jobs first."
        return RedirectResponse("/collect#scoring", 303)

    @app.post("/jobs/{job_id:int}/answer")
    async def answer(request: Request, job_id: int) -> Response:
        form = request.state.form
        if form.get("label") not in ANSWERS:
            raise HTTPException(400, "Choose yes, maybe or no, or clear the mark.")
        if not actions.set_answer(request.app.state.conn, queries.known(job_id), form["label"]):
            raise HTTPException(404)
        return _to_job(job_id, form)

    @app.post("/jobs/{job_id:int}/not-interested")
    async def not_interested(request: Request, job_id: int) -> Response:
        form = request.state.form
        if not actions.set_not_interested(request.app.state.conn, queries.known(job_id), form.get("value") == "1"):
            raise HTTPException(404)
        return _to_job(job_id, form)

    @app.post("/jobs/{job_id:int}/location")
    async def fix_location(request: Request, job_id: int) -> Response:
        form = request.state.form
        if form.get("choice") not in ("yes", "no", "auto"):
            raise HTTPException(400, "Choose US, not US, or let collections decide again.")
        if not actions.set_location(request.app.state.conn, queries.known(job_id), form["choice"]):
            raise HTTPException(404)
        return _to_job(job_id, form)

    @app.get("/add")
    async def add_page(request: Request) -> Response:
        return _page(request, "add.html", form={})

    @app.post("/add/job")
    async def add_job(request: Request) -> Response:
        form = request.state.form
        message, job_id = await run_in_threadpool(  # beside the dashboard: sites can take a while to answer
            actions.add_job, data_dir, request.app.state.collector.fetcher, form.get("link", ""),
            form.get("company", ""), form.get("title", ""),
        )  # fmt: skip
        return _page(request, "add.html", form=form, job_message=message, job_id=job_id)

    @app.post("/add/company")
    async def find_company(request: Request) -> Response:
        form = request.state.form
        fetcher = request.app.state.collector.fetcher  # finds only; nothing is saved until "Add" is pressed
        boards, notes = await run_in_threadpool(actions.find_company, data_dir, fetcher, form.get("name", ""))
        return _page(request, "add.html", form=form, found=boards, company_lines=notes)

    @app.post("/add/board")
    async def add_board(request: Request) -> Response:
        fetcher, board = request.app.state.collector.fetcher, request.state.form.get("board", "")
        line = await run_in_threadpool(actions.add_board, data_dir, fetcher, board)
        return _page(request, "add.html", form={}, company_lines=[line])

    @app.post("/boards/stop")
    async def stop_board(request: Request) -> Response:
        form = request.state.form
        if not actions.set_board_stopped(request.app.state.conn, form.get("board", ""), form.get("value") == "1"):
            raise HTTPException(404)
        job, back = form.get("job", ""), form.get("back", "")
        if DIGITS.fullmatch(job):
            return _to_job(queries.known(int(job)), form)
        return RedirectResponse(_boards_back(back) if back.startswith("/boards") else "/settings", 303)

    @app.get("/boards")
    async def boards_page(request: Request) -> Response:
        return _boards_page(request)

    @app.post("/boards/confirm")
    async def confirm_board(request: Request) -> Response:
        fetcher, board = request.app.state.collector.fetcher, request.state.form.get("board", "")
        line = await run_in_threadpool(actions.add_board, data_dir, fetcher, board)  # the same as Add
        return _boards_page(request, line=line)

    @app.post("/boards/decline")
    async def decline_board(request: Request) -> Response:
        form = request.state.form
        if not actions.decline_board(request.app.state.conn, form.get("board", "")):
            raise HTTPException(404)
        return RedirectResponse(_boards_back(form.get("back", "")), 303)

    @app.get("/collect")
    async def collect_page(request: Request) -> Response:
        collector, scorer = request.app.state.collector, request.app.state.scorer
        notice, collector.notice = collector.notice, None  # shown once
        score_notice, scorer.notice = scorer.notice, None
        return _collect_page(request, refused=notice, score_refused=score_notice)

    @app.post("/collect")
    async def collect_now(request: Request) -> Response:
        request.app.state.collector.notice = request.app.state.collector.start()
        return RedirectResponse("/collect", 303)  # so reloading the page never starts another collection

    @app.post("/phonebook/refresh")
    async def refresh_now(request: Request) -> Response:
        request.app.state.collector.notice = request.app.state.collector.start("refresh")
        return RedirectResponse("/collect", 303)  # so reloading the page never starts another refresh

    @app.get("/collect/progress")
    async def collect_progress(request: Request) -> Response:
        return _page(request, "collect_progress.html", collector=request.app.state.collector)

    @app.get("/settings")
    async def settings_page(request: Request) -> Response:
        return await _settings_page(request)

    @app.post("/settings")
    async def save_settings(request: Request) -> Response:
        problem, scorer = actions.save_settings(request.state.form), request.app.state.scorer
        if not problem and scorer.automatic and not load_config().scoring.automatic:
            scorer.cancel()  # automatic scoring switched off: its run stops after the current job
        typed = request.state.form if problem else None  # a refused save keeps what was typed
        return await _settings_page(request, problem=problem, typed=typed)

    prepare.add_routes(app, _page, data_dir, lambda: _scoring())  # looked up at each call, as the routes above do
    writer_settings.add_routes(app, _page)
    council_settings.add_routes(app, _page)
    council_view.add_routes(app, _page, lambda: _scoring())
    pilot.add_routes(app, _page)
    drafts.add_routes(app, _page, data_dir)
    return app


async def _schedule(app: FastAPI) -> None:
    """The schedule's check, once a minute while the dashboard is open."""
    while True:
        await asyncio.sleep(SCHEDULE_CHECK_SECONDS)
        for runner in (app.state.collector, app.state.scorer, app.state.preparer):  # collections first
            try:
                runner.tick(app.state.conn)
            except Exception as error:  # e.g. the database busy for a moment: try again next minute
                log.error("Schedule check failed: %s", type(error).__name__)


def _scoring() -> queries.Scoring:
    """The chosen model, the ready threshold and the resume's fingerprint, as they are now."""
    try:
        config = load_config()
    except ConfigError:  # shown on the Settings page; the defaults meanwhile
        return queries.Scoring()
    try:
        resume = read_resume(config.data_dir).fingerprint
    except ResumeError:
        resume = None
    return queries.Scoring(config.ai.evaluator.model, config.scoring.ready_threshold, resume)


async def _installed() -> list[str]:
    """The models downloaded in Ollama on this computer; none when Ollama is off or the evaluator is a cloud service."""
    try:
        role = load_config().ai.evaluator
    except ConfigError:
        return []
    return await run_in_threadpool(ollama.installed, role) if role.service == "ollama" else []


def _collect_page(request: Request, refused: str | None = None, score_refused: str | None = None) -> HTMLResponse:
    resume_problem = None
    try:
        config, settings_error = load_config(), None
        every, refresh_every, automatic = config.collect_every_hours, config.phonebook_every_days, config.scoring
        read_resume(config.data_dir)
    except ConfigError as error:
        every, refresh_every, automatic, settings_error = 0, 0, None, str(error)
    except ResumeError as error:  # why nothing scores, before anyone presses Score now
        resume_problem = str(error)
    conn = request.app.state.conn
    return _page(
        request, "collect.html", collector=request.app.state.collector, refused=refused, runs=queries.last_runs(conn),
        every=every, refresh_every=refresh_every, refreshes=queries.last_refreshes(conn), settings_error=settings_error,
        scorer=request.app.state.scorer, score_refused=score_refused, scoring=automatic, resume_problem=resume_problem,
    )  # fmt: skip


def _boards_page(request: Request, line: str | None = None) -> HTMLResponse:
    conn, view = request.app.state.conn, queries.BoardView.from_address(dict(request.query_params))
    boards, total, number, pages = queries.list_boards(conn, view)
    return _page(
        request, "boards.html", view=view, boards=boards, total=total, number=number, pages=pages,
        counts=queries.board_counts(conn), waiting=queries.waiting_boards(conn), empty=queries.empty_boards(conn),
        line=line,
        collector=request.app.state.collector,
    )  # fmt: skip


def _boards_back(address: str) -> str:
    """The Boards view to go back to: only the page's own addresses, else its first view."""
    return address if address.startswith("/boards?") and len(address) <= 1000 else "/boards"


async def _settings_page(
    request: Request, problem: str | None = None, typed: dict[str, str] | None = None
) -> HTMLResponse:
    try:
        config = load_config()  # a settings file that breaks the rules is shown as a problem, never edited
        form = actions.settings_form() | {name: value for name, value in (typed or {}).items() if name != "data_dir"}
    except ConfigError as error:
        return _page(request, "settings.html", file_problem=str(error))
    rows = sum(1 for name in form if actions.LIST_ROW.fullmatch(name)) + 1  # every list, plus one empty row
    if config.ai.evaluator.service == "ollama":
        _, ollama_line = await run_in_threadpool(ollama.status, config.ai.evaluator)  # waits up to 5 s for Ollama
    else:  # a cloud service is never asked from here
        ollama_line = "Phase 3b scores jobs only on this computer: set ai.evaluator.service to ollama."
    return _page(
        request, "settings.html", form=form, problem=problem, saved=request.method == "POST" and not problem,
        rows=rows, stopped=queries.stopped_boards(request.app.state.conn), ollama_line=ollama_line,
    )  # fmt: skip


async def _checked(request: Request, call_next) -> Response:
    host = request.headers.get("host", "")
    if urlsplit(f"//{host}").hostname not in LOCAL_HOSTS:  # stops "DNS rebinding" by other websites
        raise HTTPException(
            400, "This address is not allowed. Open the dashboard at the address shown in the terminal."
        )
    post = request.method == "POST"
    if post and request.headers.get("origin", f"http://{host}") != f"http://{host}":  # sent from another website
        raise HTTPException(403, EXPIRED)
    request.state.session = None
    path = request.url.path
    if path != "/login" and not path.startswith("/static/"):  # everything else needs a login
        request.state.session = request.app.state.logins.session(request.cookies.get(security.COOKIE))
        if request.state.session is None:
            return _to_login(request)
    if post:
        request.state.form = await _form(request)
        sent = request.state.form.get("csrf_token") or request.headers.get("x-csrf-token")
        if request.state.session and not security.token_matches(sent, request.state.session):
            raise HTTPException(403, EXPIRED)
    return await call_next(request)


async def _form(request: Request) -> dict[str, str]:
    """A small text form, read with Python's own parser; the last value wins for a repeated name."""
    size = request.headers.get("content-length", "0")
    if not DIGITS.fullmatch(size):
        raise HTTPException(400, "The form could not be read.")
    limit = MAX_UPLOAD_BYTES if request.url.path == UPLOAD else MAX_FORM_BYTES
    if int(size) > limit:
        raise HTTPException(413, "Too much data.")
    body = bytearray()
    async for chunk in request.stream():  # piece by piece, so a huge upload is refused before it fills memory
        body += chunk
        if len(body) > limit:
            raise HTTPException(413, "Too much data.")
    try:
        fields = parse_qs(body.decode(), keep_blank_values=True, max_num_fields=500)
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(400, "The form could not be read.") from None
    return {name: values[-1] for name, values in fields.items()}


def _back(address: str) -> str:
    """The list view to go back to: only the dashboard's own list addresses, else the first view."""
    return address if address.startswith("/?") and len(address) <= 4000 else "/"


def _in_view(address: str, choice: str) -> str:
    """A list page shown in `choice`: only the dashboard's own list pages, else the Jobs list. The view goes in the
    address too, so the page shows it even when the browser keeps no cookies."""
    parts = urlsplit(address)
    if parts.scheme or parts.netloc or parts.path not in LISTS or len(address) > 4000:
        parts = urlsplit("/")
    pairs = [pair for pair in parse_qsl(parts.query, keep_blank_values=True) if pair[0] != "view"]
    return f"{parts.path}?{urlencode([*pairs, ('view', choice)])}"


def _view(request: Request) -> str:
    """The job lists' view: as the page address asks, else as this browser keeps it, else Cards (also for an unknown
    or damaged value)."""
    asked = (request.query_params.get("view"), request.cookies.get(VIEW_COOKIE))
    return next((found for found in asked if found in VIEWS), "cards")


def _look(request: Request) -> tuple[str, str]:
    """This browser's colours and mode; an unknown or damaged cookie gives the defaults (teal, as the device)."""
    scheme, _, mode = request.cookies.get(LOOK_COOKIE, "").partition(".")
    return (scheme, mode) if scheme in SCHEMES and mode in MODES else ("today", "auto")


def _to_job(job_id: int, form: dict[str, str]) -> Response:
    """Back to the job's page after a change, keeping the way back to the list."""
    return RedirectResponse(f"/jobs/{job_id}?back={quote(_back(form.get('back', '')), safe='')}", 303)


def _to_login(request: Request) -> Response:
    if request.headers.get("hx-request"):  # HTMX swaps answers into the page, so it is told to load the login page
        return Response(status_code=204, headers={"HX-Redirect": "/login"})
    return RedirectResponse("/login", 303)


def _page(request: Request, name: str, status: int = 200, **values) -> HTMLResponse:
    session = getattr(request.state, "session", None)
    values["csrf"] = session.csrf if session else None
    # worked out only by a page that shows it (base.html), never by the boxes it redraws
    values["topbar"] = partial(council_view.topbar, request.app.state.conn) if session else None
    values["list_view"] = _view(request)
    values["look"] = _look(request)
    return templates.TemplateResponse(request, name, values, status_code=status)


def _error(request: Request, status: int, message: str) -> HTMLResponse:
    if status == 404:
        message = "Page not found."
    return _page(request, "error.html", status, message=message)
