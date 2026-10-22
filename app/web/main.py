"""The dashboard web app: safety checks on every request, login, and the pages."""

import logging
from contextlib import asynccontextmanager, closing
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException

from app import db
from app.web import security

HERE = Path(__file__).parent
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}  # the dashboard listens on this computer only
MAX_FORM_BYTES = 64 * 1024
EXPIRED = "This page has expired; reload it and try again."
log = logging.getLogger("app.web")
templates = Jinja2Templates(directory=HERE / "templates")  # escapes every value shown in a page


def create_app(data_dir: Path, logins: security.Logins | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with closing(db.connect(data_dir)) as conn:  # opened here, so pages use it from the server's own thread
            app.state.conn = conn
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.logins = logins or security.Logins()
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
            log.error("Unexpected error: %s", type(error).__name__)
            response = _error(
                request,
                500,
                f"Something went wrong ({type(error).__name__}); nothing was saved. "
                "Try again; if it keeps happening, restart the dashboard.",
            )
        response.headers.update(security.SAFETY_HEADERS)
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
            token = logins.log_in(request.state.form.get("password", ""), row[0])
            if token:
                response = RedirectResponse("/", 303)
                response.set_cookie(security.COOKIE, token, httponly=True, samesite="strict", path="/")
                return response
        minutes = logins.locked_minutes()
        error = f"Too many wrong passwords. Try again in {minutes} minute(s)." if minutes else "Wrong password."
        return _page(request, "login.html", 429 if minutes else 401, error=error)

    @app.post("/logout")
    async def log_out(request: Request) -> Response:
        request.app.state.logins.log_out(request.cookies.get(security.COOKIE))
        response = RedirectResponse("/login", 303)
        response.delete_cookie(security.COOKIE, path="/")
        return response

    @app.get("/")
    async def home(request: Request) -> Response:
        return _page(request, "home.html")

    return app


async def _checked(request: Request, call_next) -> Response:
    host = request.headers.get("host", "")
    if urlsplit(f"//{host}").hostname not in ALLOWED_HOSTS:  # stops "DNS rebinding" by other websites
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
    if int(request.headers.get("content-length") or 0) > MAX_FORM_BYTES:
        raise HTTPException(413, "Too much data.")
    body = await request.body()
    if len(body) > MAX_FORM_BYTES:
        raise HTTPException(413, "Too much data.")
    try:
        fields = parse_qs(body.decode(), keep_blank_values=True, max_num_fields=500)
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(400, "The form could not be read.") from None
    return {name: values[-1] for name, values in fields.items()}


def _to_login(request: Request) -> Response:
    if request.headers.get("hx-request"):  # HTMX swaps answers into the page, so it is told to load the login page
        return Response(status_code=204, headers={"HX-Redirect": "/login"})
    return RedirectResponse("/login", 303)


def _page(request: Request, name: str, status: int = 200, **values) -> HTMLResponse:
    session = getattr(request.state, "session", None)
    values["csrf"] = session.csrf if session else None
    return templates.TemplateResponse(request, name, values, status_code=status)


def _error(request: Request, status: int, message: str) -> HTMLResponse:
    if status == 404:
        message = "Page not found."
    return _page(request, "error.html", status, message=message)
