"""The browser plugin's door (/plugin/), kept apart from main.py: a key and a pinned plugin in place of a login. It
reads no cookie, so the plugin can't keep a login alive, and adds no CORS header: that is what keeps job pages out."""

import hashlib
import hmac
import json
import re
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException
from starlette.requests import ClientDisconnect

from app import collect
from app.config import ConfigError, load_config
from app.submit import fills, preview
from app.web import queries

PREFIX = "/plugin/"
HELLO = PREFIX + "hello"
PROTOCOL = 1
MAX_BODY = 1024 * 1024
MAX_VERSION = 32
MAX_ADDRESS = 2_000
BROWSER = "SELECT name FROM paired_browsers WHERE id = ?"
ORIGIN = re.compile(r"chrome-extension://[a-p]{32}")
VERSION = re.compile(r"[0-9]+(\.[0-9]+){0,3}")  # as Chrome writes one; \d would also take other scripts' digits
NO_KEY, WRONG_PLUGIN, OLD_PLUGIN = "no-key", "wrong-plugin", "old-plugin"
TOO_BIG, BAD_REQUEST, APP_ERROR = "too-big", "bad-request", "app-error"
# every refusal the plugin can get, from the agreement between the two; anything else is never passed on
KINDS = {NO_KEY, WRONG_PLUGIN, OLD_PLUGIN, TOO_BIG, BAD_REQUEST, APP_ERROR}
NO_TICKET, NOT_LOGGED_IN, NOT_READY, NO_JOB = "no-ticket", "not-logged-in", "not-ready", "no-job"
KINDS |= {NO_TICKET, NOT_LOGGED_IN, NOT_READY, NO_JOB}


def key_hash(key: str) -> str:
    """A plain hash: a slow one protects guessable passwords, and nobody can guess 256 random bits."""
    return hashlib.sha256(key.encode()).hexdigest()


def refusal(status: int, message: str) -> JSONResponse:
    """A kind from the list, whatever the words were: an error here never holds page text."""
    kind = message if message in KINDS else APP_ERROR if status >= 500 else BAD_REQUEST
    return JSONResponse({"error": kind}, status)


def admit(request: Request) -> None:
    """The key, then the plugin, for every method and before any body is read; refuses by raising."""
    conn = request.app.state.conn
    scheme, _, key = request.headers.get("authorization", "").partition(" ")
    sent = key_hash(key)  # hashed first, so odd characters can't crash the compare
    browsers = conn.execute("SELECT id, key_hash, plugin FROM paired_browsers").fetchall()
    found = next((row for row in browsers if hmac.compare_digest(row[1], sent)), None)
    if scheme != "Bearer" or found is None:
        raise HTTPException(401, NO_KEY)
    browser_id, _, pinned = found
    origin = request.headers.get("origin", "")
    if not ORIGIN.fullmatch(origin):  # before pinning, so a web page with a stolen key never gets itself pinned
        raise HTTPException(403, WRONG_PLUGIN)
    if pinned is None:
        with conn:  # only if still free: of two first calls at once, one wins
            conn.execute("UPDATE paired_browsers SET plugin = ? WHERE id = ? AND plugin IS NULL", (origin, browser_id))
        pinned = conn.execute("SELECT plugin FROM paired_browsers WHERE id = ?", (browser_id,)).fetchone()[0]
    if pinned != origin:
        raise HTTPException(403, WRONG_PLUGIN)
    request.state.browser_id = browser_id


async def read_json(request: Request) -> dict:
    """A JSON object, read piece by piece, so a huge body is refused before it fills memory."""
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY:
            raise HTTPException(413, TOO_BIG)
    try:
        parsed = json.loads(body.decode())
        json.dumps(parsed, ensure_ascii=False).encode()  # a lone surrogate like \ud800 would crash a later save
    except (ValueError, RecursionError):
        raise HTTPException(400, BAD_REQUEST) from None
    if not isinstance(parsed, dict):
        raise HTTPException(400, BAD_REQUEST)
    return parsed


async def read_call(request: Request) -> dict:
    """A call's body, from a plugin that speaks this protocol and a browser still paired once the body has arrived,
    which can take a while: its name is kept for the trail."""
    try:
        body = await read_json(request)
    except ClientDisconnect:  # nobody is left to answer, and nothing went wrong here
        raise HTTPException(400, BAD_REQUEST) from None
    if "protocol" not in body:
        raise HTTPException(400, BAD_REQUEST)
    if (type(body["protocol"]), body["protocol"]) != (int, PROTOCOL):  # type too: in Python, True == 1
        raise HTTPException(400, OLD_PLUGIN)
    found = request.app.state.conn.execute(BROWSER, (request.state.browser_id,)).fetchone()
    if found is None:
        raise HTTPException(401, NO_KEY)
    request.state.browser_name = found[0]
    return body


def unpair(request: Request, browser_id: int | None = None) -> None:
    """Removes this browser, or every browser, and ends its fills, all in one save: the delete goes first, so a busy
    database stops the press with nothing done."""
    conn, tickets = request.app.state.conn, request.app.state.tickets
    with conn:
        if browser_id is None:
            conn.execute("DELETE FROM paired_browsers")
            tickets.end_all(conn)
        else:
            conn.execute("DELETE FROM paired_browsers WHERE id = ?", (browser_id,))
            tickets.end_browser(conn, browser_id)


def add_routes(app: FastAPI, data_dir: Path) -> None:
    @app.post(HELLO)
    async def hello(request: Request) -> Response:
        body = await read_call(request)
        version = body.get("version")
        if not (isinstance(version, str) and len(version) <= MAX_VERSION and VERSION.fullmatch(version)):
            raise HTTPException(400, BAD_REQUEST)
        conn = request.app.state.conn
        with conn:  # only hello writes here, so later calls never wait on a busy database for this
            conn.execute(
                "UPDATE paired_browsers SET version = ?, last_used = ? WHERE id = ?",
                (version, collect.now(), request.state.browser_id),
            )
        return JSONResponse({"app": "auto_apply", "protocol": PROTOCOL})

    @app.post(PREFIX + "start")
    async def start(request: Request) -> Response:
        address = _address(await read_call(request))
        state = request.state
        ticket = request.app.state.tickets.take(request.app.state.conn, address, state.browser_id, state.browser_name)
        if ticket is None:
            raise HTTPException(404, NO_TICKET)
        return JSONResponse({"ticket": ticket.id, "mode": "fill", "board": ticket.site})

    @app.post(PREFIX + "known")
    async def known(request: Request) -> Response:
        body = await read_call(request)
        _logged_in(request)
        conn = request.app.state.conn
        job_id = fills.job_at(conn, _address(body))
        if job_id is None:
            return JSONResponse({"job": None})
        job = queries.job(conn, job_id)
        return JSONResponse({"job": f"/jobs/{job_id}", "title": job["title"], "company": job["company"]})

    @app.post(PREFIX + "fill")
    async def fill(request: Request) -> Response:
        body = await read_call(request)
        _logged_in(request)
        conn, state = request.app.state.conn, request.state
        job_id = fills.job_at(conn, _address(body))
        if job_id is None:
            raise HTTPException(404, NO_JOB)
        try:
            limits = load_config()
        except ConfigError:
            return _not_ready(job_id)
        job, found = queries.job(conn, job_id), preview.preview(conn, data_dir, job_id)
        reason = request.app.state.tickets.make(conn, job, found, limits, state.browser_id, state.browser_name)
        return JSONResponse({"ok": True}) if reason is None else _not_ready(job_id)


def _address(body: dict) -> str:
    address = body.get("address")
    if not (isinstance(address, str) and len(address) <= MAX_ADDRESS):
        raise HTTPException(400, BAD_REQUEST)
    return address


def _logged_in(request: Request) -> None:
    """Only looks: a key alone, or a key with a login that has run out, can't start anything."""
    if not request.app.state.logins.live():
        raise HTTPException(403, NOT_LOGGED_IN)


def _not_ready(job_id: int) -> JSONResponse:
    """The one refusal with more than its kind: the job's page says why, so the plugin sends you there."""
    return JSONResponse({"error": NOT_READY, "job": f"/jobs/{job_id}"}, 409)
