"""Settings → Gmail (/settings/gmail), kept apart from main.py: your own Google client, Connect (read-only, with a
one-time check), Google's answer, and Disconnect. The client secret and the lasting sign-in go only into the keychain:
never shown, never in a file, never in a page or an address."""

import base64
import hashlib
import hmac
import re
import secrets
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app import collect
from app.gmail.client import AUTH_URL, CLIENT_ID, CLIENT_SECRET, REFRESH, SCOPE, Gmail, GmailError, exchange, revoke
from app.keychain import KeychainError, delete_secret, get_secret, set_secret

PAGE = "/settings/gmail"
CALLBACK = PAGE + "/callback"
PENDING_MINUTES = 10  # how long a Connect waits for Google's answer
VISIBLE = re.compile(r"[\x21-\x7e]{1,200}")  # as Google gives them: visible characters, no spaces
DONE = {  # ?done=…: fixed words only
    "client": "Google client saved. Now press Connect.",
    "syncing": "Syncing now; this page shows the result when you reload it.",
    "no-client": "Save your Google client ID and secret first.",
    "disconnected": "Disconnected: the app can no longer read your Gmail.",
    "disconnected-here": (
        "Disconnected here, but Google could not be told; you can remove the app's access in your Google account's "
        "security page."
    ),
}
STALE = "This sign-in is no longer valid (used, replaced or older than 10 minutes); press Connect again."
CONNECTED = "UPDATE gmail_sync SET connected_on = ? WHERE id = 1"
_now = time.monotonic  # tests replace it


def add_routes(app: FastAPI, page: Callable[..., HTMLResponse]) -> None:
    app.state.gmail_transport = None  # tests set a fake Google
    app.state.gmail_pending = None  # the one Connect waiting for Google's answer: state, verifier, redirect, created

    @app.get(PAGE)
    async def gmail_page(request: Request) -> Response:
        return await _view(request, page, notice=DONE.get(request.query_params.get("done", "")))

    @app.post(PAGE + "/client")
    async def save_client(request: Request) -> Response:
        client_id, client_secret = request.state.form.get("client_id", ""), request.state.form.get("client_secret", "")
        if not (VISIBLE.fullmatch(client_id) and client_id.endswith(".apps.googleusercontent.com")):
            return await _view(request, page, 400, problem="The client ID ends with .apps.googleusercontent.com.")
        if not VISIBLE.fullmatch(client_secret):
            return await _view(request, page, 400, problem="Paste the client secret as Google shows it: no spaces.")
        try:
            set_secret(CLIENT_ID, client_id)
            set_secret(CLIENT_SECRET, client_secret)
        except KeychainError as error:
            return await _view(request, page, 400, problem=str(error))
        return RedirectResponse(f"{PAGE}?done=client", 303)

    @app.get(PAGE + "/connect")
    async def connect(request: Request) -> Response:
        """A link, not a form: the safety rules let a form go only to this dashboard, and this answer goes to Google."""
        try:
            client_id = get_secret(CLIENT_ID) if get_secret(CLIENT_SECRET) else None
        except KeychainError as error:
            return await _view(request, page, 400, problem=str(error))
        if not client_id:
            return RedirectResponse(f"{PAGE}?done=no-client", 303)
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        redirect = f"http://127.0.0.1:{request.url.port}{CALLBACK}"
        request.app.state.gmail_pending = {
            "state": state,
            "verifier": verifier,
            "redirect": redirect,
            "created": _now(),
        }
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        return RedirectResponse(AUTH_URL + "?" + urlencode({
            "client_id": client_id, "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
            "access_type": "offline", "prompt": "consent", "state": state, "code_challenge": challenge,
            "code_challenge_method": "S256",
        }), 303)  # fmt: skip

    @app.get(CALLBACK)
    async def google_answer(request: Request) -> Response:
        """Google sends you back here (without the login cookie); the one-time state is the check."""
        pending, request.app.state.gmail_pending = request.app.state.gmail_pending, None  # used once, also on failure
        params = request.query_params
        if (
            pending is None
            or not hmac.compare_digest(pending["state"].encode(), params.get("state", "").encode())
            or _now() - pending["created"] > PENDING_MINUTES * 60
        ):
            return page(request, "gmail_done.html", 400, sentence=STALE)
        if "error" in params:
            return page(request, "gmail_done.html", 400, sentence="Google sign-in was cancelled; nothing was saved.")
        try:
            client_id, client_secret = get_secret(CLIENT_ID), get_secret(CLIENT_SECRET)
            if not (client_id and client_secret):
                raise GmailError(DONE["no-client"])
            sign_in = (params.get("code", ""), client_id, client_secret, pending["redirect"], pending["verifier"])
            token = await run_in_threadpool(exchange, *sign_in, request.app.state.gmail_transport)
            set_secret(REFRESH, token)
        except (GmailError, KeychainError) as error:
            return page(request, "gmail_done.html", 400, sentence=str(error))
        conn = request.app.state.conn
        with conn:
            conn.execute("INSERT OR IGNORE INTO gmail_sync (id) VALUES (1)")
            conn.execute(CONNECTED, (collect.now(),))
        return page(request, "gmail_done.html", sentence="Gmail connected. The app can now read your mail (read-only).")

    @app.post(PAGE + "/sync")
    async def sync_now(request: Request) -> Response:
        request.app.state.mailer.wake()
        return RedirectResponse(f"{PAGE}?done=syncing", 303)

    @app.post(PAGE + "/disconnect")
    async def disconnect(request: Request) -> Response:
        """Google is asked to forget the sign-in, then it is removed here either way; e-mails already read stay."""
        try:
            token = get_secret(REFRESH)
            told = await run_in_threadpool(revoke, token, request.app.state.gmail_transport) if token else True
            delete_secret(REFRESH)
        except KeychainError as error:
            return await _view(request, page, 400, problem=str(error))
        conn = request.app.state.conn
        with conn:
            conn.execute(CONNECTED, (None,))
        return RedirectResponse(f"{PAGE}?done={'disconnected' if told else 'disconnected-here'}", 303)


async def _view(request: Request, page: Callable[..., HTMLResponse], status: int = 200, **values) -> HTMLResponse:
    """The page: whether your client is saved and Gmail connected (never the values), since when, the address (asked
    of Gmail, never stored) and the last sync."""
    try:
        sign_in = get_secret(CLIENT_ID), get_secret(CLIENT_SECRET), get_secret(REFRESH)
    except KeychainError as error:
        sign_in, values["problem"] = (None, None, None), values.get("problem") or str(error)
    values["has_client"], values["connected"] = bool(sign_in[0] and sign_in[1]), sign_in[2] is not None
    row = request.app.state.conn.execute("SELECT connected_on, last_sync_on, last_result FROM gmail_sync").fetchone()
    connected_on, last_sync, values["last_result"] = row or (None, None, None)
    if connected_on and values["connected"]:
        since = datetime.fromisoformat(connected_on).astimezone().date()
        values["since"], values["again"] = since, since + timedelta(days=7)
    if last_sync:
        values["last_sync"] = f"{datetime.fromisoformat(last_sync).astimezone():%Y-%m-%d %H:%M}"
    mailer_notice = request.app.state.mailer.notice
    values["latest"] = mailer_notice if mailer_notice != values["last_result"] else None  # e.g. "busy elsewhere"
    if all(sign_in):
        values["address"], values["address_problem"] = await run_in_threadpool(
            _address, sign_in, request.app.state.gmail_transport
        )
    return page(request, "gmail_settings.html", status, **values)


def _address(sign_in: tuple, transport: httpx.BaseTransport | None) -> tuple[str | None, str | None]:
    """The connected account's address, or why it can't be read now."""
    try:
        with Gmail(*sign_in, transport=transport) as gmail:
            return gmail.profile()[0], None
    except GmailError as error:
        return None, str(error)
