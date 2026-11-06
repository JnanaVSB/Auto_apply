"""The browser plugin's door (/plugin/), kept apart from main.py: a key and a pinned plugin in place of a login. It
reads no cookie, so the plugin can't keep a login alive, and adds no CORS header: that is what keeps job pages out."""

import hashlib
import hmac
import json
import re

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException

from app import collect

PREFIX = "/plugin/"
HELLO = PREFIX + "hello"
PROTOCOL = 1
MAX_BODY = 1024 * 1024
MAX_VERSION = 32
ORIGIN = re.compile(r"chrome-extension://[a-p]{32}")
VERSION = re.compile(r"[0-9]+(\.[0-9]+){0,3}")  # as Chrome writes one; \d would also take other scripts' digits
NO_KEY, WRONG_PLUGIN, OLD_PLUGIN = "no-key", "wrong-plugin", "old-plugin"
TOO_BIG, BAD_REQUEST, APP_ERROR = "too-big", "bad-request", "app-error"
# every refusal the plugin can get, from the agreement between the two; anything else is never passed on
KINDS = {NO_KEY, WRONG_PLUGIN, OLD_PLUGIN, TOO_BIG, BAD_REQUEST, APP_ERROR}
KINDS |= {"no-ticket", "not-logged-in", "not-ready", "no-job"}  # used from chunk 2 on


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


def add_routes(app: FastAPI) -> None:
    @app.post(HELLO)
    async def hello(request: Request) -> Response:
        body = await read_json(request)
        if "protocol" not in body:
            raise HTTPException(400, BAD_REQUEST)
        if (type(body["protocol"]), body["protocol"]) != (int, PROTOCOL):  # type too: in Python, True == 1
            raise HTTPException(400, OLD_PLUGIN)
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
