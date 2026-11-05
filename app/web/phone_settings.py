"""Settings → Phone (/settings/phone), kept apart from main.py: the address Tailscale gives this laptop. It is changed
only on the laptop, so a phone can't change or blank the address that lets it in."""

from collections.abc import Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.config import ConfigError, load_config
from app.web import actions, security

PAGE = "/settings/phone"


def add_routes(app: FastAPI, page: Callable[..., HTMLResponse]) -> None:
    @app.get(PAGE)
    async def phone_page(request: Request) -> Response:
        return _view(request, page)

    @app.post(PAGE)
    async def save_address(request: Request) -> Response:
        security.laptop_only(request, "Change the phone address on the laptop.")
        typed = request.state.form.get("phone_address", "").strip()
        if problem := actions.update_settings(lambda old: old | {"phone_address": typed}):
            return _view(request, page, 400, problem=problem, typed=typed)
        return RedirectResponse(PAGE, 303)


def _view(
    request: Request,
    page: Callable[..., HTMLResponse],
    status: int = 200,
    problem: str | None = None,
    typed: str | None = None,
) -> HTMLResponse:
    saved, restart = "", False
    try:
        saved = load_config().phone_address
        restart = urlsplit(saved).netloc != request.app.state.phone_host  # read once, at the start
    except ConfigError as error:  # a settings file that breaks the rules is shown as a problem, never edited
        problem = problem or str(error)
    box = saved if typed is None else typed
    return page(request, "phone_settings.html", status, saved=saved, box=box, restart=restart, problem=problem)
