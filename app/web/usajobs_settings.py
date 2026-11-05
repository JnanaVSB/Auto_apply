"""Settings → USAJobs key (/settings/usajobs), kept apart from main.py: your own key and e-mail go only into the
keychain and are never shown; they leave this computer only in the headers of a request to USAJobs."""

import re
from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response

from app.config import Config
from app.keychain import KeychainError, delete_secret, get_secret, set_secret
from app.sources.usajobs import EMAIL, KEY
from app.web import security
from app.web.writer_settings import KEY as KEY_SHAPE

PAGE = "/settings/usajobs"
ON_THE_LAPTOP = "Change your USAJobs key on the laptop."
EMAIL_SHAPE = re.compile(r"[!-?A-~]+@[!-?A-~]+")  # visible plain characters, as a request's headers must carry it
MAX_EMAIL = 254


def needs_key(config: Config) -> bool:
    """USAJobs is ticked, but a collection would skip it. A failing keychain is said on the key page."""
    try:
        return "usajobs" in config.sources.feeds and not (get_secret(KEY) and get_secret(EMAIL))
    except KeychainError:
        return False


def add_routes(app: FastAPI, page: Callable[..., HTMLResponse]) -> None:
    @app.get(PAGE)
    async def usajobs_page(request: Request) -> Response:
        security.laptop_only(request, ON_THE_LAPTOP)
        return _view(request, page)

    @app.post(PAGE)
    async def save_key(request: Request) -> Response:
        security.laptop_only(request, ON_THE_LAPTOP)
        key, email = request.state.form.get("key", ""), request.state.form.get("email", "").strip()
        if not (key or email):
            return _view(request, page, 400, problem="Not saved: type your key, your e-mail, or both.")
        if key and not KEY_SHAPE.fullmatch(key):
            return _view(request, page, 400, problem="Not saved: paste the whole key, without spaces.")
        if email and not (EMAIL_SHAPE.fullmatch(email) and len(email) <= MAX_EMAIL):
            return _view(request, page, 400, problem="Not saved: that doesn't look like one e-mail address.")
        try:
            for name, value in ((KEY, key), (EMAIL, email)):
                if value:
                    set_secret(name, value)
        except KeychainError as error:
            return _view(request, page, problem=str(error))
        return _view(request, page, notice="Saved.")

    @app.post(PAGE + "/remove")
    async def remove_key(request: Request) -> Response:
        security.laptop_only(request, ON_THE_LAPTOP)
        try:
            delete_secret(KEY)
            delete_secret(EMAIL)
        except KeychainError as error:
            return _view(request, page, problem=str(error))
        return _view(request, page, notice="Removed.")


def _view(
    request: Request,
    page: Callable[..., HTMLResponse],
    status: int = 200,
    problem: str | None = None,
    notice: str | None = None,
) -> HTMLResponse:
    key = email = False
    try:
        key, email = bool(get_secret(KEY)), bool(get_secret(EMAIL))
    except KeychainError as error:
        problem = problem or str(error)
    return page(request, "usajobs_settings.html", status, key=key, email=email, problem=problem, notice=notice)
