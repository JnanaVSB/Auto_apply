"""Settings → Browser plugin (/settings/plugin), kept apart from main.py: each browser's name and the hash of its key.
A key is shown once, in the answer to the Add press, and is never kept or logged."""

import secrets
import unicodedata
from collections.abc import Callable

import sqlcipher3
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app import collect
from app.submit import fills
from app.web import actions, plugin

PAGE = "/settings/plugin"
MAX_NAME = 40
BAD_NAME = f"Not added: type a name of 1 to {MAX_NAME} characters, with no tabs or line breaks."
NAME_TAKEN = "That name is already in the list. Choose another. If you didn't copy its key, remove it and add it again."
BROWSERS = "SELECT id, name, added_on, last_used, version, plugin FROM paired_browsers ORDER BY id"
ADD = "INSERT INTO paired_browsers (name, key_hash, added_on) VALUES (?, ?, ?)"


def add_routes(app: FastAPI, page: Callable[..., HTMLResponse]) -> None:
    @app.get(PAGE)
    async def plugin_page(request: Request) -> Response:
        return _view(request, page)

    @app.post(PAGE)
    async def add_browser(request: Request) -> Response:
        name = request.state.form.get("name", "").strip()
        if not 1 <= len(name) <= MAX_NAME or any(unicodedata.category(letter) == "Cc" for letter in name):
            return _view(request, page, 400, problem=BAD_NAME)
        key, conn = secrets.token_urlsafe(32), request.app.state.conn
        try:
            with conn:
                conn.execute(ADD, (name, plugin.key_hash(key), collect.now()))
        except sqlcipher3.IntegrityError:  # also a reload: no redirect after Add, or the key would be lost
            return _view(request, page, 400, problem=NAME_TAKEN)
        return _view(request, page, key=key, added=name)

    @app.post(PAGE + "/remove")
    async def remove_browser(request: Request) -> Response:
        browser_id = actions.whole(request.state.form.get("id", ""))
        if isinstance(browser_id, int):  # anything else, from the form, is no browser
            plugin.unpair(request, browser_id)
        return RedirectResponse(PAGE, 303)


def _view(
    request: Request,
    page: Callable[..., HTMLResponse],
    status: int = 200,
    problem: str | None = None,
    key: str | None = None,
    added: str | None = None,
) -> HTMLResponse:
    conn = request.app.state.conn
    browsers, recent = conn.execute(BROWSERS).fetchall(), fills.recent(conn)
    return page(
        request, "plugin_settings.html", status, browsers=browsers, recent=recent, problem=problem, key=key, added=added
    )
