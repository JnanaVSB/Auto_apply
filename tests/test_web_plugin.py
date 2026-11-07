"""The browser plugin's door: pairing a browser, and who may come in on /plugin/. Fake keys and made-up plugins only."""

import base64
import hashlib
import json
import logging
import os
import re
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import sqlcipher3
from fastapi import Request
from fastapi.testclient import TestClient
from helpers import ADDRESS, add_job, csrf, log_in, one
from starlette.exceptions import HTTPException
from test_web_phone import FORWARDED, FUNNEL, HOST, IDENTITY, ORIGIN, PHONE, phone_login, words

from app import collect, config, submit
from app.config import Config
from app.documents import pdf
from app.fetch import Fetcher
from app.submit import Field, Form, fill_words, fills, match, preview
from app.web import main, preparer, queries, security

HELLO = "/plugin/hello"
START = "/plugin/start"
KNOWN = "/plugin/known"
FILL = "/plugin/fill"
FORM = "/plugin/form"
REPORT = "/plugin/report"
PAPER = "/plugin/file"
ROUTES = (HELLO, START, KNOWN, FILL, FORM, REPORT, PAPER)
SETTINGS = "/settings/plugin"
REMOVE = "/settings/plugin/remove"
LOG_OUT_ALL = "/settings/phone/log-out-all"
KEY = "fake-plugin-key-not-real"
PLUGIN = "chrome-extension://" + "a" * 32
OTHER_PLUGIN = "chrome-extension://" + "p" * 32
JOB_PAGE = "http://127.0.0.1:8767"
GOOD = {"protocol": 1, "version": "1.2.3"}
LINK = "https://job-boards.greenhouse.io/acme/jobs/123"
FORM_ADDRESS = "https://job-boards.greenhouse.io/embed/job_app?for=acme&token=123"
FIRST_NAME = Field("first_name", "First Name", "text", True, "standard")
SECOND_KEY = "second-fake-key"
FILLS = "SELECT toolbar, browser, state, stop_kind FROM fills ORDER BY id"
NAME_BOX = {"key": "first_name", "label": "First Name*", "kind": "text", "required": True}
NAME_PACK = {"key": "first_name", "kind": "text", "value": "Fake"}
RESUME_FIELD = Field("resume", "Resume/CV", "file", True, "standard")
LETTER_FIELD = Field("cover_letter", "Cover Letter", "file", False, "standard")
RESUME_BOX = {"key": "resume", "label": "Resume/CV", "kind": "file", "required": True}
LETTER_BOX = {"key": "cover_letter", "label": "Cover Letter", "kind": "file", "required": False}
RESUME_PDF, LETTER_PDF = b"%PDF-1.7 a fake resume", b"%PDF-1.7 a fake letter"
DONE = """INSERT INTO preparations (job_id, kind, version, sources, writer, status, draft, created_on)
    VALUES (?, ?, ?, '[]', '{}', 'done', '{}', 'then')"""
STOP = {"go": False, "reason": "unexpected-page"}
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD")
BAD_ORIGINS = (
    None,
    "",
    "null",
    JOB_PAGE,
    "https://jobs.example",
    PLUGIN + "/",
    PLUGIN + "\n",
    PLUGIN + "a",
    "chrome-extension://" + "a" * 31,
    "chrome-extension://" + "a" * 33,
    "chrome-extension://" + "q" * 32,
    "chrome-extension://" + "A" * 32,
    "moz-extension://" + "a" * 32,
    "http://" + "a" * 32,
    "x" + PLUGIN,
)
SHOWN_KEY = re.compile(r'<code id="key" class="address">([\w-]+)</code>')
NAME_TAKEN = "That name is already in the list. Choose another. If you didn't copy its key, remove it and add it again."
BAD_NAME = "Not added: type a name of 1 to 40 characters, with no tabs or line breaks."
WRONG_PLUGIN_HINT = "If the plugin says it was refused, remove that browser here and add it again."
LOG_OUT_ALL_HINT = "This also removes every browser in Settings → Browser plugin: each one will need a new key."
NOT_FINISHED = "A plugin is linked, but didn't finish connecting"
COMPARE_HINT = (
    "Compare each plugin ID with the ID on your browser's extensions page: if it's different, remove that browser."
)
SHOWN_ID = '<code class="address">' + "a" * 32 + "</code>"
BUSY = "The database is busy"


def hashed(key=KEY):
    return hashlib.sha256(key.encode()).hexdigest()


def pair(conn, key=KEY, name="Laptop Chrome", plugin=None):
    with conn:
        return conn.execute(
            "INSERT INTO paired_browsers (name, key_hash, plugin, added_on) VALUES (?, ?, ?, ?)",
            (name, hashed(key), plugin, "2026-11-05T10:00:00+00:00"),
        ).lastrowid


def row(conn, browser_id=1):
    return one(conn, "SELECT plugin, version, last_used FROM paired_browsers WHERE id = ?", browser_id)


def count(conn):
    return one(conn, "SELECT count(*) FROM paired_browsers")[0]


def sent(key=KEY, origin=PLUGIN):
    """The key, and the Origin unless it is None, for the browser sending none."""
    return {"Authorization": f"Bearer {key}"} | ({} if origin is None else {"Origin": origin})


def refused(response, status, kind):
    return (response.status_code, response.json()) == (status, {"error": kind})


@pytest.fixture
def door(client):
    """The same running dashboard with no login cookie, as the plugin calls it."""
    plugin_side = TestClient(client.app, base_url=ADDRESS)
    plugin_side.portal = client.portal  # the database belongs to that thread
    return plugin_side


@pytest.fixture
def owner(client):
    log_in(client)
    return client


@pytest.fixture
def phone_dashboard(data_dir, clock, web, monkeypatch):
    """The dashboard with a phone address, and a client that comes in the way Tailscale Serve brings it."""
    monkeypatch.setattr(preparer.Preparer, "wake", lambda self: None)
    with ExitStack() as running:
        app = main.create_app(
            data_dir, security.Logins(clock=clock), lambda: Fetcher(httpx.MockTransport(web)), phone_address=PHONE
        )
        laptop = running.enter_context(TestClient(app, base_url=ADDRESS))
        phone = TestClient(app, base_url=f"http://{HOST}", headers={"Host": HOST} | IDENTITY | FORWARDED)
        phone.portal = laptop.portal
        yield laptop, phone


def raw(client, method, path, headers=None, chunks=()):
    """Straight to the app: the test client would tidy `..` away and read a whole body before sending it.

    Returns the status, the answer's text and how many times the app asked for more of the body.
    """
    reads, answer = [], {"body": b""}
    waiting = iter(chunks)

    async def receive():
        reads.append(1)
        chunk = next(waiting, None)
        if chunk is None:
            return {"type": "http.request", "body": b"", "more_body": False}
        if isinstance(chunk, dict):  # a message of its own, such as the connection dropping
            return chunk
        return {"type": "http.request", "body": chunk, "more_body": True}

    async def send(message):
        if message["type"] == "http.response.start":
            answer["status"] = message["status"]
        else:
            answer["body"] += message.get("body", b"")

    sent_headers = {"host": "127.0.0.1:8765"} | {name.lower(): value for name, value in (headers or {}).items()}
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(name.encode(), value.encode()) for name, value in sent_headers.items()],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8765),
    }
    client.portal.call(client.app, scope, receive, send)
    return answer["status"], answer["body"].decode(), len(reads)


# The door: every method needs the key and the pinned plugin


def test_a_new_browser_says_hello_and_is_pinned_to_its_plugin(door, conn):
    pair(conn)
    response = door.post(HELLO, json=GOOD, headers=sent())
    assert (response.status_code, response.json()) == (200, {"app": "auto_apply", "protocol": 1})
    plugin, version, last_used = row(conn)
    assert (plugin, version) == (PLUGIN, "1.2.3") and last_used


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("path", [*ROUTES, "/plugin/nothing-here"])
def test_every_method_needs_the_key(door, conn, method, path):
    pair(conn, plugin=PLUGIN)
    pair(conn, "another-fake-key", "Removed one")
    with conn:
        conn.execute("DELETE FROM paired_browsers WHERE name = 'Removed one'")
    for headers in (
        {},
        {"Origin": PLUGIN},
        sent("a-wrong-fake-key"),
        sent("another-fake-key"),
        {"Authorization": KEY, "Origin": PLUGIN},
        {"Authorization": f"Basic {KEY}", "Origin": PLUGIN},
        {"Authorization": "Bearer ", "Origin": PLUGIN},
        {"Authorization": b"Bearer caf\xe9", "Origin": PLUGIN},
    ):
        response = door.request(method, path, headers=headers)
        assert response.status_code == 401
        if method != "HEAD":
            assert response.json() == {"error": "no-key"}
    assert row(conn)[1:] == (None, None)


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("path", ROUTES)
def test_every_method_needs_the_pinned_plugin(door, conn, method, path):
    pair(conn, plugin=PLUGIN)
    for origin in (*BAD_ORIGINS, OTHER_PLUGIN):
        response = door.request(method, path, headers=sent(origin=origin))
        assert response.status_code == 403
        if method != "HEAD":
            assert response.json() == {"error": "wrong-plugin"}
    assert row(conn) == (PLUGIN, None, None)


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("path", ROUTES)
def test_a_bad_origin_never_pins_a_new_browser(door, conn, method, path):
    """On a browser with no pin the shape check alone decides: nothing else stands behind it."""
    pair(conn)
    for origin in BAD_ORIGINS:
        response = door.request(method, path, json=GOOD, headers=sent(origin=origin))
        assert response.status_code == 403
        if method != "HEAD":
            assert response.json() == {"error": "wrong-plugin"}
    assert row(conn) == (None, None, None)
    assert door.post(HELLO, json=GOOD, headers=sent()).status_code == 200  # the real plugin still can be
    assert row(conn)[0] == PLUGIN


@pytest.mark.parametrize(
    ("method", "path", "status"), [("GET", "/plugin/nothing", 404), ("GET", HELLO, 405), ("OPTIONS", HELLO, 405)]
)
def test_a_first_call_pins_on_any_path_and_method(door, conn, method, path, status):
    pair(conn)
    assert refused(door.request(method, path, headers=sent()), status, "bad-request")
    assert row(conn) == (PLUGIN, None, None)


def test_the_door_leaves_no_session_for_a_route_to_find(door, conn):
    pair(conn, plugin=PLUGIN)

    @door.app.get("/plugin/session-probe")
    async def probe(request: Request):
        return {"session": request.state.session}

    response = door.get("/plugin/session-probe", headers=sent())
    assert (response.status_code, response.json()) == (200, {"session": None})


def test_a_browser_stays_with_the_plugin_it_was_pinned_to(door, conn):
    pair(conn)
    assert door.post(HELLO, json=GOOD, headers=sent()).status_code == 200
    assert refused(door.post(HELLO, json=GOOD, headers=sent(origin=OTHER_PLUGIN)), 403, "wrong-plugin")
    assert row(conn)[0] == PLUGIN


class RacingFirstCall:
    """The dashboard's connection, with another first call getting in just before this one pins."""

    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, *values):
        if sql.startswith("UPDATE paired_browsers SET plugin"):
            self.conn.execute("UPDATE paired_browsers SET plugin = ?", (PLUGIN,))
        return self.conn.execute(sql, *values)

    def __enter__(self):
        return self.conn.__enter__()

    def __exit__(self, *problem):
        return self.conn.__exit__(*problem)


def test_of_two_first_calls_at_once_one_wins(door, conn, monkeypatch):
    pair(conn)
    monkeypatch.setattr(door.app.state, "conn", RacingFirstCall(door.app.state.conn))
    assert refused(door.post(HELLO, json=GOOD, headers=sent(origin=OTHER_PLUGIN)), 403, "wrong-plugin")
    assert row(conn)[0] == PLUGIN


def test_two_browsers_each_have_their_own_key_and_plugin(door, conn):
    pair(conn, plugin=PLUGIN)
    pair(conn, "second-fake-key", "Second", plugin=OTHER_PLUGIN)
    assert door.post(HELLO, json=GOOD, headers=sent("second-fake-key", OTHER_PLUGIN)).status_code == 200
    assert (row(conn, 1)[1], row(conn, 2)[1]) == (None, "1.2.3")
    assert refused(door.post(HELLO, json=GOOD, headers=sent("second-fake-key", PLUGIN)), 403, "wrong-plugin")


@pytest.mark.parametrize("site", ["same-site", "cross-site"])
@pytest.mark.parametrize("path", ROUTES)
def test_a_job_pages_simple_post_is_refused_and_writes_nothing(door, conn, site, path):
    """Piece 0 showed this one reaches the server: only the door can stop it."""
    pair(conn)
    headers = {"Origin": JOB_PAGE, "Content-Type": "text/plain;charset=UTF-8", "Sec-Fetch-Site": site}
    for mode in ("cors", "no-cors"):
        response = door.post(path, content="hello=page", headers=headers | {"Sec-Fetch-Mode": mode})
        assert refused(response, 401, "no-key")
    assert row(conn) == (None, None, None)


def test_a_login_cookie_alone_opens_nothing(owner, conn):
    pair(conn, plugin=PLUGIN)
    assert refused(owner.post(HELLO, json=GOOD), 401, "no-key")
    assert refused(owner.post(HELLO, json=GOOD, headers={"Origin": PLUGIN}), 401, "no-key")
    assert refused(owner.get("/plugin/nothing-here"), 401, "no-key")


def test_the_plugin_never_keeps_a_login_alive(owner, conn, clock):
    pair(conn, plugin=PLUGIN)
    session = owner.app.state.logins.sessions[owner.cookies[security.COOKIE]]
    seen = session.last_seen
    clock.now += 100
    assert owner.post(HELLO, json=GOOD, headers=sent()).status_code == 200
    assert owner.post(HELLO, json=GOOD).status_code == 401
    assert session.last_seen == seen


def test_wrong_keys_never_count_towards_the_password_lock_out(door, conn):
    pair(conn)
    for _ in range(security.MAX_WRONG * 3):
        door.post(HELLO, json=GOOD, headers=sent("a-wrong-fake-key"))
    logins = door.app.state.logins
    assert (logins.wrong, logins.locked_minutes(), logins.sessions) == (0, 0, {})


def test_a_valid_key_with_no_cookie_opens_nothing_outside_the_door(door, conn):
    pair(conn, plugin=PLUGIN)
    for path in ("/", "/settings", SETTINGS, "/jobs/1", "/plugin"):
        response = door.get(path, headers=sent(), follow_redirects=False)
        assert (response.status_code, response.headers["location"]) == (303, "/login")
    for path in (SETTINGS, REMOVE, LOG_OUT_ALL):
        response = door.post(
            path, data={"name": "Sneaked in", "id": "1"}, headers=sent(origin=None), follow_redirects=False
        )
        assert (response.status_code, response.headers["location"]) == (303, "/login")
    assert one(conn, "SELECT name FROM paired_browsers") == ("Laptop Chrome",)


@pytest.mark.parametrize(
    "path",
    ["/plugin/../settings", "/plugin/..//settings", "/plugin/./settings", "/plugin/../", "/plugin/%2e%2e/settings"],
)
def test_path_tricks_reach_nothing(owner, conn, path):
    pair(conn, plugin=PLUGIN)
    status, text, _ = raw(owner, "GET", path, {"Origin": PLUGIN})
    assert (status, text) == (401, '{"error":"no-key"}')
    status, text, _ = raw(owner, "GET", path, sent())
    assert (status, text) == (404, '{"error":"bad-request"}')


def test_near_misses_of_the_door_are_ordinary_paths(door, conn):
    pair(conn, plugin=PLUGIN)
    for path in ("/plugin", "/plugins/hello", "/PLUGIN/hello", "//plugin/hello"):
        status, text, _ = raw(door, "GET", path, sent())
        assert (status, text) == (303, "")


@pytest.mark.parametrize("path", ROUTES)
def test_no_body_is_read_before_the_door_has_decided(door, conn, path):
    pair(conn, plugin=PLUGIN)
    big = [b"x" * 2**20, b"x" * 2**20]
    headers = {"Content-Length": str(2 * 2**20), "Content-Type": "application/json"}
    for extra in ({}, {"Authorization": "Bearer a-wrong-fake-key", "Origin": PLUGIN}):
        status, text, reads = raw(door, "POST", path, headers | extra, big)
        assert (status, text, reads) == (401, '{"error":"no-key"}', 0)
    status, text, reads = raw(door, "POST", path, headers | sent(origin=JOB_PAGE), big)
    assert (status, text, reads) == (403, '{"error":"wrong-plugin"}', 0)


# /plugin/hello


@pytest.mark.parametrize(
    "body", [GOOD | {"version": "bad"}, GOOD | {"protocol": 2}, {"version": "1.2.3"}, ["not an object"]]
)
def test_a_refused_first_hello_pins_but_saves_no_version_and_no_time(door, conn, body):
    pair(conn)
    assert door.post(HELLO, json=body, headers=sent()).status_code == 400
    assert row(conn) == (PLUGIN, None, None)


def test_a_repeated_hello_changes_only_the_time(door, conn, monkeypatch):
    pair(conn)
    monkeypatch.setattr(collect, "now", lambda: "2026-11-05T11:00:00+00:00")
    door.post(HELLO, json=GOOD, headers=sent())
    assert row(conn) == (PLUGIN, "1.2.3", "2026-11-05T11:00:00+00:00")
    monkeypatch.setattr(collect, "now", lambda: "2026-11-05T12:00:00+00:00")
    door.post(HELLO, json=GOOD, headers=sent())
    assert row(conn) == (PLUGIN, "1.2.3", "2026-11-05T12:00:00+00:00")
    assert count(conn) == 1


@pytest.mark.parametrize("protocol", [2, 0, "1", True, 1.0, None, [1]])
@pytest.mark.parametrize("path", ROUTES)
def test_any_other_protocol_is_an_old_plugin(door, conn, protocol, path):
    pair(conn, plugin=PLUGIN)
    assert refused(door.post(path, json=GOOD | {"protocol": protocol}, headers=sent()), 400, "old-plugin")
    assert row(conn)[1:] == (None, None)


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"{",
        b"[1, 2]",
        b'"text"',
        b"1",
        b"null",
        b'{"version": "1.2.3"}',  # no protocol
        b'{"protocol": 1}',  # no version
        b'\xff{"protocol": 1, "version": "1.2.3"}',
        '{"protocol": 1, "version": "1.2.3"}'.encode("utf-16"),
        b'{"protocol": 1, "version": "1.2.3", "note": "\xe9"}',
        b'{"protocol": 1, "version": "1.2.3", "note": "\\ud800"}',
        b'{"protocol": 1, "version": "1.2.3", "\\ud800": "key"}',
        b"[" * 200_000,
        b'{"protocol": 1, "version": "1.2.3", "note": ' + b"[" * 200_000 + b"]" * 200_000 + b"}",
    ],
)
def test_a_damaged_body_is_refused_and_nothing_is_saved(door, conn, body):
    pair(conn, plugin=PLUGIN)
    assert refused(door.post(HELLO, content=body, headers=sent()), 400, "bad-request")
    assert row(conn)[1:] == (None, None)


@pytest.mark.parametrize(
    "version",
    [
        "",
        "1.2.3.4.5",
        "1.",
        ".1",
        "1..2",
        "1.2.3-beta",
        " 1.2",
        "1.2\n",
        "١.٢.٣",  # Arabic-Indic digits: Python's \d takes them
        "1" * 33,
        ".".join(["12345678"] * 4) + "9",
        1,
        1.2,
        None,
        ["1.2"],
    ],
)
def test_a_version_that_is_no_chrome_version_is_refused(door, conn, version):
    pair(conn, plugin=PLUGIN)
    assert refused(door.post(HELLO, json=GOOD | {"version": version}, headers=sent()), 400, "bad-request")
    assert row(conn)[1:] == (None, None)


@pytest.mark.parametrize("version", ["1", "1.2", "1.2.3", "131.0.6778.85", "1" * 32, "0.0.0.1"])
def test_chrome_versions_are_saved(door, conn, version):
    pair(conn, plugin=PLUGIN)
    assert door.post(HELLO, json=GOOD | {"version": version}, headers=sent()).status_code == 200
    assert row(conn)[1] == version


def test_a_body_past_one_megabyte_is_too_big_with_or_without_a_length(door, conn):
    pair(conn, plugin=PLUGIN)
    padded = b'{"protocol": 1, "version": "1.2.3", "note": "' + b"x" * 2**20 + b'"}'
    assert refused(door.post(HELLO, content=padded, headers=sent()), 413, "too-big")

    def pieces():
        yield padded

    assert refused(door.post(HELLO, content=pieces(), headers=sent()), 413, "too-big")
    assert row(conn)[1:] == (None, None)


def test_the_reader_stops_as_soon_as_a_body_is_too_big(door, conn):
    pair(conn, plugin=PLUGIN)
    pieces = [b"x" * 2**19] * 6
    status, text, reads = raw(door, "POST", HELLO, sent(), pieces)
    assert (status, text, reads) == (413, '{"error":"too-big"}', 3)


def test_a_body_of_exactly_one_megabyte_is_read(door, conn):
    pair(conn, plugin=PLUGIN)
    start, end = b'{"protocol": 1, "version": "1.2.3", "note": "', b'"}'
    body = start + b"x" * (2**20 - len(start) - len(end)) + end
    assert door.post(HELLO, content=body, headers=sent()).status_code == 200


# Refusals are JSON, from a fixed list, with no page text


# The reader, for every route


def call(door, path, address=FORM_ADDRESS, key=KEY):
    return door.post(path, json={"protocol": 1, "address": address}, headers=sent(key))


def ready_job(conn, link=LINK, title="ML Intern", **values):
    """A job with its form read and every line answered, so it can be filled."""
    with conn:
        job = add_job(conn, title, apply_url=link, **values)
    submit.save(conn, job, Form((FIRST_NAME,)), "api")
    preview.keep(conn, job, FIRST_NAME, "Fake")
    return job


def press(door, conn, data_dir, job):
    """Open and fill: a ticket for whichever browser opens the form."""
    limits = Config(data_dir=data_dir, fill_pause_minutes=0)
    found = preview.preview(conn, data_dir, job)
    assert door.app.state.tickets.make(conn, queries.job(conn, job), found, limits) is None


def fill_rows(conn):
    return conn.execute(FILLS).fetchall()


def held(door):
    return door.app.state.tickets.tickets


@pytest.fixture
def limits(tmp_path, data_dir, monkeypatch):
    """Writes the settings /plugin/fill reads at each call; no pause and the usual cap until a test says otherwise."""
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(config, "CONFIG_PATH", path)

    def write(cap=10, pause=0):
        path.write_text(f"data_dir: {data_dir}\nfills_per_day: {cap}\nfill_pause_minutes: {pause}\n", encoding="utf-8")

    write()
    return write


@pytest.mark.parametrize("path", ROUTES)
@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"[1]",
        b"{",
        b'{"address": "x"}',
        b'{"version": "1.2.3"}',
        b'{"note": "\\ud800", "protocol": 1}',
    ],
)
def test_every_route_refuses_a_damaged_body(owner, door, conn, path, body):
    pair(conn, plugin=PLUGIN)
    assert refused(door.post(path, content=body, headers=sent()), 400, "bad-request")
    assert fill_rows(conn) == [] and not held(door)


@pytest.mark.parametrize("path", [START, KNOWN, FILL])
@pytest.mark.parametrize("address", [None, 1, True, 1.5, [FORM_ADDRESS], {"address": FORM_ADDRESS}, "x" * 2001])
def test_an_address_must_be_a_string_of_at_most_2000_characters(owner, door, conn, limits, path, address):
    pair(conn, plugin=PLUGIN)
    ready_job(conn)
    assert refused(call(door, path, address), 400, "bad-request")
    assert fill_rows(conn) == [] and not held(door)


@pytest.mark.parametrize("path", [START, KNOWN, FILL])
def test_no_address_is_a_bad_request(owner, door, conn, path):
    pair(conn, plugin=PLUGIN)
    assert refused(door.post(path, json={"protocol": 1}, headers=sent()), 400, "bad-request")


def test_an_address_of_2000_characters_is_only_unknown(owner, door, conn):
    pair(conn, plugin=PLUGIN)
    assert refused(call(door, START, "x" * 2000), 404, "no-ticket")
    assert call(door, KNOWN, "x" * 2000).json() == {"job": None}
    assert refused(call(door, FILL, "x" * 2000), 404, "no-job")


@pytest.mark.parametrize("path", ROUTES)
def test_a_browser_removed_while_its_body_is_read_gets_nothing(owner, door, conn, data_dir, limits, path):
    gone = pair(conn, plugin=PLUGIN)
    pair(conn, SECOND_KEY, "Second", plugin=PLUGIN)
    press(door, conn, data_dir, ready_job(conn))
    body = json.dumps(GOOD | {"address": FORM_ADDRESS}).encode()

    def sending():
        yield body[:10]
        with door.app.state.conn as app_conn:  # in the dashboard's own thread, as a Remove press would be
            app_conn.execute("DELETE FROM paired_browsers WHERE id = ?", (gone,))
        yield body[10:]

    status, text, _ = raw(door, "POST", path, sent(), sending())
    assert (status, text) == (401, '{"error":"no-key"}')
    assert fill_rows(conn) == [(0, None, "waiting", None)]
    assert not next(iter(held(door).values())).taken


@pytest.mark.parametrize("path", ROUTES)
def test_a_dropped_connection_is_quiet(door, conn, caplog, path):
    pair(conn, plugin=PLUGIN)
    caplog.set_level(logging.DEBUG)
    status, text, _ = raw(door, "POST", path, sent(), [b"{", {"type": "http.disconnect"}])
    assert (status, text) == (400, '{"error":"bad-request"}')
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


# /plugin/known and /plugin/fill need a live login


@pytest.mark.parametrize("path", [KNOWN, FILL])
def test_the_key_alone_makes_nothing(door, conn, limits, path):
    pair(conn, plugin=PLUGIN)
    ready_job(conn)
    assert refused(call(door, path), 403, "not-logged-in")
    assert fill_rows(conn) == [] and not held(door)


@pytest.mark.parametrize("path", [KNOWN, FILL])
@pytest.mark.parametrize("gone", [security.IDLE_SECONDS, security.MAX_SESSION_SECONDS])
def test_a_timed_out_login_is_no_login(owner, door, conn, clock, limits, path, gone):
    pair(conn, plugin=PLUGIN)
    ready_job(conn)
    clock.now += gone + 1
    assert refused(call(door, path), 403, "not-logged-in")
    assert fill_rows(conn) == [] and not held(door)


@pytest.mark.parametrize("path", [KNOWN, FILL])
def test_the_plugin_s_calls_keep_no_login_alive(owner, door, conn, clock, limits, path):
    pair(conn, plugin=PLUGIN)
    ready_job(conn)
    (session,) = owner.app.state.logins.sessions.values()
    seen = session.last_seen
    clock.now += 60
    assert call(door, path).status_code == 200
    assert session.last_seen == seen


def test_a_known_form_gives_the_job(owner, door, conn):
    pair(conn, plugin=PLUGIN)
    job = ready_job(conn)
    assert call(door, KNOWN).json() == {"job": f"/jobs/{job}", "title": "ML Intern", "company": "Acme"}


def test_an_unknown_form_gives_no_job(owner, door, conn):
    pair(conn, plugin=PLUGIN)
    response = call(door, KNOWN, "https://jobs.lever.co/acme/1/apply")
    assert (response.status_code, response.json()) == (200, {"job": None})


NEAR_MISSES = (
    LINK,
    FORM_ADDRESS.replace("https:", "http:"),
    FORM_ADDRESS + "/",
    FORM_ADDRESS + "&x=1",
    FORM_ADDRESS + "#top",
    FORM_ADDRESS + " ",
    " " + FORM_ADDRESS,
    FORM_ADDRESS.replace("for=acme", "for=ACME"),
    FORM_ADDRESS.replace("job-boards", "JOB-BOARDS"),
    FORM_ADDRESS.replace("job-boards", "boards"),
    "https://job-boards.greenhouse.io/embed/job_app?token=123&for=acme",
    "",
)


@pytest.mark.parametrize("address", NEAR_MISSES)
def test_a_near_miss_finds_no_job(owner, door, conn, data_dir, limits, address):
    pair(conn, plugin=PLUGIN)
    ready_job(conn)
    assert call(door, KNOWN, address).json() == {"job": None}
    assert refused(call(door, FILL, address), 404, "no-job")
    press(door, conn, data_dir, 1)
    assert refused(call(door, START, address), 404, "no-ticket")


@pytest.mark.parametrize("address", ["https://[", "http://[::1", "https://℀.greenhouse.io/a/jobs/1"])
def test_a_broken_address_is_simply_no_job(owner, door, conn, limits, caplog, address):
    pair(conn, plugin=PLUGIN)
    caplog.set_level(logging.DEBUG)
    response = call(door, KNOWN, address)
    assert (response.status_code, response.json()) == (200, {"job": None})
    assert refused(call(door, FILL, address), 404, "no-job")
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


def test_a_job_opened_by_hand_is_no_job(owner, door, conn, limits):
    pair(conn, plugin=PLUGIN)
    ready_job(conn, open_manually_only=1)
    assert call(door, KNOWN).json() == {"job": None}
    assert refused(call(door, FILL), 404, "no-job")


# /plugin/fill


def not_ready(response, job):
    return (response.status_code, response.json()) == (409, {"error": "not-ready", "job": f"/jobs/{job}"})


def test_the_toolbar_makes_a_fill_only_its_browser_can_start(owner, door, conn, limits):
    pair(conn, plugin=PLUGIN)
    pair(conn, SECOND_KEY, "Second", plugin=OTHER_PLUGIN)
    ready_job(conn)
    response = call(door, FILL)
    assert (response.status_code, response.json()) == (200, {"ok": True})
    assert fill_rows(conn) == [(1, "Laptop Chrome", "waiting", None)]
    other = door.post(START, json={"protocol": 1, "address": FORM_ADDRESS}, headers=sent(SECOND_KEY, OTHER_PLUGIN))
    assert refused(other, 404, "no-ticket")
    assert fill_rows(conn) == [(1, "Laptop Chrome", "waiting", None)]
    started = call(door, START)
    assert started.status_code == 200
    assert fill_rows(conn) == [(1, "Laptop Chrome", "started", None)]


def test_a_repeat_toolbar_press_makes_no_second_row(owner, door, conn, limits):
    pair(conn, plugin=PLUGIN)
    ready_job(conn)
    limits(pause=2)
    assert call(door, FILL).status_code == 200
    assert call(door, FILL).status_code == 200
    assert len(fill_rows(conn)) == 1


def test_a_form_never_read_is_not_ready(owner, door, conn, limits):
    pair(conn, plugin=PLUGIN)
    with conn:
        job = add_job(conn, "ML Intern", apply_url=LINK)
    assert not_ready(call(door, FILL), job)


def test_a_job_with_a_record_is_not_ready(owner, door, conn, limits):
    pair(conn, plugin=PLUGIN)
    job = ready_job(conn)
    with conn:
        conn.execute(
            "INSERT INTO submissions (job_id, submitted_on, saved_on, how, address, fields, files) "
            "VALUES (?, '2026-11-05', 'then', 'by hand', ?, '[]', '[]')",
            (job, FORM_ADDRESS),
        )
    assert not_ready(call(door, FILL), job)


def test_open_decisions_are_not_ready(owner, door, conn, limits):
    pair(conn, plugin=PLUGIN)
    with conn:
        job = add_job(conn, "ML Intern", apply_url=LINK)
    submit.save(conn, job, Form((Field("resume", "Resume/CV", "file", True, "standard"),)), "api")
    assert not_ready(call(door, FILL), job)
    assert fill_rows(conn) == []


def test_the_cap_and_the_pause_are_not_ready(owner, door, conn, limits):
    pair(conn, plugin=PLUGIN)
    ready_job(conn)
    second = ready_job(conn, "https://jobs.lever.co/acme/1", "Second")
    assert call(door, FILL).status_code == 200
    limits(pause=2)
    assert not_ready(call(door, FILL, "https://jobs.lever.co/acme/1/apply"), second)
    limits(cap=1)
    assert not_ready(call(door, FILL, "https://jobs.lever.co/acme/1/apply"), second)
    assert len(fill_rows(conn)) == 1


def test_settings_that_can_t_be_read_are_not_ready(owner, door, conn):
    pair(conn, plugin=PLUGIN)
    job = ready_job(conn)
    assert not_ready(call(door, FILL), job)  # no settings file in this test
    assert fill_rows(conn) == [] and not held(door)


def test_a_busy_database_makes_no_fill(owner, door, conn, limits):
    pair(conn, plugin=PLUGIN)
    ready_job(conn)
    conn.execute("BEGIN IMMEDIATE")
    response = call(door, FILL)
    conn.rollback()
    assert refused(response, 503, "app-error")
    assert fill_rows(conn) == [] and not held(door)


# /plugin/start


def test_a_button_ticket_starts_once_for_the_exact_address(door, conn, data_dir, paired):
    press(door, conn, data_dir, ready_job(conn))
    assert refused(call(door, START, FORM_ADDRESS + "&x=1"), 404, "no-ticket")
    response = call(door, START)
    ticket = next(iter(held(door).values()))
    assert (response.status_code, response.json()) == (
        200,
        {"ticket": ticket.id, "mode": "fill", "board": "greenhouse", "thanks": None},
    )
    assert fill_rows(conn) == [(0, "Laptop Chrome", "started", None)]
    assert refused(call(door, START), 404, "no-ticket")  # a reload finds nothing


def test_start_with_no_ticket_is_no_ticket(door, paired):
    assert refused(call(door, START), 404, "no-ticket")


def test_a_ticket_past_its_time_can_t_start(door, conn, data_dir, clock, paired):
    press(door, conn, data_dir, ready_job(conn))
    clock.now += fills.UNUSED_SECONDS + 1
    assert refused(call(door, START), 404, "no-ticket")


def test_a_busy_database_gives_app_error_and_uses_no_ticket(door, conn, data_dir, paired):
    press(door, conn, data_dir, ready_job(conn))
    conn.execute("BEGIN IMMEDIATE")
    response = call(door, START)
    conn.rollback()
    assert refused(response, 503, "app-error")
    assert fill_rows(conn) == [(0, None, "waiting", None)]
    assert call(door, START).status_code == 200


# /plugin/form


def send(door, ticket, fields, key=KEY, origin=PLUGIN):
    return door.post(FORM, json={"protocol": 1, "ticket": ticket, "fields": fields}, headers=sent(key, origin))


def running(door, conn, data_dir, job=None):
    """A fill this browser has started; its ticket."""
    press(door, conn, data_dir, job or ready_job(conn))
    return call(door, START).json()["ticket"]


def changes(conn):
    return json.loads(one(conn, "SELECT changes FROM fills ORDER BY id DESC")[0])


@pytest.fixture
def paired(owner, conn):
    """A connected browser, with you logged in: a fill runs only under a live login."""
    return pair(conn, plugin=PLUGIN)


def test_a_form_that_matches_gets_its_answers_once(door, conn, data_dir, paired):
    ticket = running(door, conn, data_dir)
    response = send(door, ticket, [NAME_BOX])
    assert (response.status_code, response.json()) == (200, {"go": True, "pack": [NAME_PACK]})
    assert fill_rows(conn) == [(0, "Laptop Chrome", "sent", None)] and changes(conn) == []
    assert refused(send(door, ticket, [NAME_BOX]), 404, "no-ticket")  # a reload can't fill again


def test_what_was_left_out_is_kept_on_the_row(door, conn, data_dir, paired):
    ticket = running(door, conn, data_dir)
    extra = {"key": "question_1", "label": "Why Acme?", "kind": "long_text", "required": False}
    assert send(door, ticket, [NAME_BOX, extra]).json() == {"go": True, "pack": [NAME_PACK]}
    assert changes(conn) == [{"question": 'The form shows "Why Acme?"', "what": "new on the form"}]


def test_an_ashby_form_read_by_the_browser_fills_too(door, conn, data_dir, paired):
    link = "https://jobs.ashbyhq.com/acme/0a1b-2c3d"
    with conn:
        job = add_job(conn, "ML Intern", apply_url=link)
    field = Field("_systemfield_name", "Name", "text", True, "standard")
    submit.save(conn, job, Form((field,)), "browser")
    preview.keep(conn, job, field, "Fake Person")
    press(door, conn, data_dir, job)
    answer = call(door, START, f"{link}/application").json()
    assert answer["board"] == "ashby"
    drawn_box = {"key": "_systemfield_name", "label": "Name*", "kind": "text", "required": True}
    assert send(door, answer["ticket"], [drawn_box]).json()["pack"] == [
        {"key": "_systemfield_name", "kind": "text", "value": "Fake Person"}
    ]


def test_the_answers_are_read_when_the_form_is_sent(door, conn, data_dir, paired):
    job = ready_job(conn)
    ticket = running(door, conn, data_dir, job)
    preview.keep(conn, job, FIRST_NAME, "Changed on the phone")
    assert send(door, ticket, [NAME_BOX]).json()["pack"] == [NAME_PACK | {"value": "Changed on the phone"}]


def test_a_line_no_longer_to_fill_stays_out_but_still_matches(door, conn, data_dir, paired):
    job = ready_job(conn)
    ticket = running(door, conn, data_dir, job)
    preview.keep(conn, job, FIRST_NAME, match.IN_WINDOW)
    assert send(door, ticket, [NAME_BOX]).json() == {"go": True, "pack": []}
    assert fill_rows(conn)[0][2:] == ("stopped", "nothing-to-fill")  # it matched: not an unexpected page


@pytest.mark.parametrize("fields", [[], [{"key": "other", "label": "Other", "kind": "text", "required": False}]])
def test_no_field_matching_stops_the_fill(door, conn, data_dir, paired, fields):
    ticket = running(door, conn, data_dir)
    assert send(door, ticket, fields).json() == STOP
    assert fill_rows(conn) == [(0, "Laptop Chrome", "stopped", "unexpected-page")]
    assert {"question": "First Name", "what": "the plugin didn't see this on the form"} in changes(conn)
    assert refused(send(door, ticket, [NAME_BOX]), 404, "no-ticket")


def test_a_saved_read_that_is_gone_stops_the_fill(door, conn, data_dir, paired):
    ticket = running(door, conn, data_dir)
    with conn:
        conn.execute("DELETE FROM forms")
    assert send(door, ticket, [NAME_BOX]).json() == STOP
    assert fill_rows(conn)[0][2:] == ("stopped", "unexpected-page")


def test_over_500_fields_are_never_read(door, conn, data_dir, paired):
    ticket = running(door, conn, data_dir)
    assert send(door, ticket, [None] * 501).json() == STOP  # nothing in them is even looked at
    assert fill_rows(conn) == [(0, "Laptop Chrome", "stopped", "too-many-fields")]
    assert refused(send(door, ticket, [NAME_BOX]), 404, "no-ticket")


def test_exactly_500_fields_are_read(door, conn, data_dir, paired):
    ticket = running(door, conn, data_dir)
    others = [{"key": f"k{number}", "label": "x", "kind": "text", "required": False} for number in range(499)]
    assert send(door, ticket, [NAME_BOX, *others]).json()["pack"] == [NAME_PACK]


def test_a_stopped_fill_stays_stopped_after_a_new_press(door, conn, data_dir, paired):
    job = ready_job(conn)
    send(door, running(door, conn, data_dir, job), [])
    press(door, conn, data_dir, job)
    assert fill_rows(conn) == [(0, "Laptop Chrome", "stopped", "unexpected-page"), (0, None, "waiting", None)]


def test_a_field_that_matches_with_nothing_to_fill_goes_with_an_empty_pack(door, conn, data_dir, paired):
    with conn:
        job = add_job(conn, "ML Intern", apply_url=LINK)
    submit.save(conn, job, Form((replace(FIRST_NAME, required=False),)), "api")
    ticket = running(door, conn, data_dir, job)
    assert send(door, ticket, [NAME_BOX | {"required": False}]).json() == {"go": True, "pack": []}


@pytest.mark.parametrize("ticket", [None, 1, True, ["x"], {"a": 1}, "x" * 2001])
def test_a_ticket_must_be_a_string_of_at_most_2000_characters(door, conn, data_dir, paired, ticket):
    running(door, conn, data_dir)
    assert refused(send(door, ticket, [NAME_BOX]), 400, "bad-request")
    assert fill_rows(conn)[0][2] == "started"


def test_no_ticket_in_the_body_is_a_bad_request(door, conn, paired):
    assert refused(door.post(FORM, json={"protocol": 1, "fields": []}, headers=sent()), 400, "bad-request")


def test_a_made_up_ticket_is_no_ticket_whatever_the_fields(door, conn, data_dir, paired):
    running(door, conn, data_dir)
    assert refused(send(door, "made-up", [NAME_BOX]), 404, "no-ticket")
    assert refused(send(door, "made-up", "not a list"), 404, "no-ticket")
    assert fill_rows(conn)[0][2] == "started"


def test_another_browser_s_ticket_is_no_ticket(door, conn, data_dir, paired):
    pair(conn, SECOND_KEY, "Second", plugin=OTHER_PLUGIN)
    ticket = running(door, conn, data_dir)
    assert refused(send(door, ticket, [NAME_BOX], SECOND_KEY, OTHER_PLUGIN), 404, "no-ticket")
    assert send(door, ticket, [NAME_BOX]).status_code == 200


def test_a_ticket_not_yet_started_is_no_ticket(door, conn, data_dir, paired):
    press(door, conn, data_dir, ready_job(conn))
    (ticket,) = held(door).values()
    assert refused(send(door, ticket.id, [NAME_BOX]), 404, "no-ticket")


def test_a_ticket_past_its_time_is_no_ticket(owner, door, conn, data_dir, clock, paired):
    ticket = running(door, conn, data_dir)
    clock.now += fills.RUNNING_SECONDS + 1
    log_in(owner)  # the first login ran out long before
    assert refused(send(door, ticket, [NAME_BOX]), 404, "no-ticket")


@pytest.mark.parametrize(
    "fields",
    [None, 1, "x", {"key": "first_name"}, [1], [NAME_BOX | {"kind": "dropdown"}], [NAME_BOX | {"key": "x" * 1001}]],
)
def test_a_bad_body_uses_nothing_up(door, conn, data_dir, paired, fields):
    ticket = running(door, conn, data_dir)
    assert refused(send(door, ticket, fields), 400, "bad-request")
    assert fill_rows(conn)[0][2] == "started"
    assert send(door, ticket, [NAME_BOX]).json() == {"go": True, "pack": [NAME_PACK]}


def test_no_fields_in_the_body_is_a_bad_request(door, conn, data_dir, paired):
    ticket = running(door, conn, data_dir)
    assert refused(door.post(FORM, json={"protocol": 1, "ticket": ticket}, headers=sent()), 400, "bad-request")


@pytest.mark.parametrize("fields", [[NAME_BOX], [], [None] * 501])
def test_a_busy_database_sends_nothing_and_uses_nothing_up(door, conn, data_dir, paired, fields):
    ticket = running(door, conn, data_dir)
    conn.execute("BEGIN IMMEDIATE")
    response = send(door, ticket, fields)
    conn.rollback()
    assert refused(response, 503, "app-error")
    assert fill_rows(conn)[0][2] == "started"
    assert send(door, ticket, [NAME_BOX]).json() == {"go": True, "pack": [NAME_PACK]}


def test_no_label_or_value_reaches_the_logs(door, conn, data_dir, paired, caplog):
    caplog.set_level(logging.DEBUG)
    ticket = running(door, conn, data_dir)
    extra = {"key": "question_1", "label": "Fake page words", "kind": "text", "required": False}
    answers = [send(door, ticket, [NAME_BOX | {"kind": "dropdown"}]), send(door, ticket, [NAME_BOX, extra])]
    assert answers[1].json()["pack"] == [NAME_PACK]
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "Fake page words" not in logged and ticket not in logged
    assert '"value": "Fake"' not in logged and "First Name" not in logged


def test_a_broken_saved_read_is_not_taken_for_a_bad_list(door, conn, data_dir, paired, monkeypatch):
    def broken(*_):
        raise ValueError("fake")

    ticket = running(door, conn, data_dir)
    assert refused(send(door, ticket, [1]), 400, "bad-request")
    monkeypatch.setattr(preview, "preview", broken)
    assert refused(send(door, ticket, [NAME_BOX]), 500, "app-error")


def log_out(owner):
    assert owner.post("/logout", data={"csrf_token": csrf(owner)}, follow_redirects=False).status_code == 303


def test_with_no_live_login_a_press_can_t_be_taken_and_nothing_is_used_up(owner, door, conn, data_dir, paired):
    press(door, conn, data_dir, ready_job(conn))
    log_out(owner)
    assert refused(call(door, START), 403, "not-logged-in")
    assert fill_rows(conn) == [(0, None, "waiting", None)]
    assert not next(iter(held(door).values())).taken
    log_in(owner)
    assert call(door, START).status_code == 200


def test_with_no_live_login_a_taken_ticket_gets_no_answers_until_you_log_in(owner, door, conn, data_dir, paired):
    ticket = running(door, conn, data_dir)
    log_out(owner)
    assert refused(send(door, ticket, [NAME_BOX]), 403, "not-logged-in")
    assert fill_rows(conn) == [(0, "Laptop Chrome", "started", None)]
    assert not next(iter(held(door).values())).answered
    log_in(owner)
    assert send(door, ticket, [NAME_BOX]).json() == {"go": True, "pack": [NAME_PACK]}


@pytest.mark.parametrize("gone", [security.IDLE_SECONDS, security.MAX_SESSION_SECONDS])
def test_a_timed_out_login_starts_nothing_and_gets_nothing(door, conn, clock, data_dir, paired, gone):
    ticket = running(door, conn, data_dir)
    press(door, conn, data_dir, ready_job(conn, "https://jobs.lever.co/acme/1", "Second"))
    clock.now += gone + 1
    assert refused(call(door, START, "https://jobs.lever.co/acme/1/apply"), 403, "not-logged-in")
    assert refused(send(door, ticket, [NAME_BOX]), 403, "not-logged-in")


def test_a_bad_body_is_still_answered_first_with_no_login(door, conn):
    pair(conn, plugin=PLUGIN)
    assert refused(door.post(START, content=b"{", headers=sent()), 400, "bad-request")
    assert refused(door.post(FORM, content=b"{", headers=sent()), 400, "bad-request")


@pytest.mark.parametrize("path", [START, KNOWN, FILL, PAPER])
def test_a_key_with_no_login_gets_nothing_from_any_route(door, conn, limits, path):
    pair(conn, plugin=PLUGIN)
    ready_job(conn)
    assert refused(call(door, path), 403, "not-logged-in")
    assert refused(send(door, "any-ticket", [NAME_BOX]), 403, "not-logged-in")


def test_starting_and_answering_keep_no_login_alive(owner, door, conn, clock, data_dir, paired):
    (session,) = owner.app.state.logins.sessions.values()
    seen = session.last_seen
    clock.now += 60
    assert send(door, running(door, conn, data_dir), [NAME_BOX]).status_code == 200
    assert session.last_seen == seen


def test_a_browser_removed_in_the_middle_of_a_fill_gets_nothing(owner, door, conn, data_dir, paired):
    pair(conn, SECOND_KEY, "Second", plugin=OTHER_PLUGIN)
    ticket = running(door, conn, data_dir)
    owner.post(REMOVE, data={"id": str(paired), "csrf_token": csrf(owner)})
    assert refused(send(door, ticket, [NAME_BOX]), 401, "no-key")
    assert refused(send(door, ticket, [NAME_BOX], SECOND_KEY, OTHER_PLUGIN), 404, "no-ticket")
    assert fill_rows(conn) == [(0, "Laptop Chrome", "ended", "browser-removed")]


def test_a_new_press_ends_a_fill_that_has_had_its_answers(door, conn, data_dir, paired):
    job = ready_job(conn)
    assert send(door, running(door, conn, data_dir, job), [NAME_BOX]).status_code == 200
    press(door, conn, data_dir, job)
    assert fill_rows(conn) == [(0, "Laptop Chrome", "sent", "replaced"), (0, None, "waiting", None)]


def test_the_answers_set_no_cookie_and_open_no_cors(owner, door, conn, data_dir, paired):
    ticket = running(door, conn, data_dir)
    answers = [send(door, ticket, [1]), send(door, ticket, [NAME_BOX]), send(door, ticket, [NAME_BOX])]
    assert [response.status_code for response in answers] == [400, 200, 404]
    for response in answers:
        assert "set-cookie" not in response.headers
        assert not [name for name in response.headers if name.lower().startswith("access-control-")]
        assert response.headers["content-type"] == "application/json"


def test_the_kinds_with_a_job_or_an_answer_hold_no_words(owner, door, conn, limits):
    pair(conn, plugin=PLUGIN)
    job = ready_job(conn)
    limits(cap=1, pause=60)
    answers = [call(door, FILL), call(door, FILL, "https://jobs.lever.co/acme/1/apply")]
    answers += [call(door, KNOWN), call(door, START), call(door, START)]
    for response in answers:
        assert "set-cookie" not in response.headers
        assert not [name for name in response.headers if name.lower().startswith("access-control-")]
        assert response.headers["content-type"] == "application/json"
    assert answers[1].json() == {"error": "no-job"}
    assert answers[2].json()["job"] == f"/jobs/{job}"
    assert answers[4].json() == {"error": "no-ticket"}


# /plugin/report


def report(door, ticket, state="filled", key=KEY, origin=PLUGIN, **body):
    whole = {"protocol": 1, "ticket": ticket, "state": state, "values": [], "left": []} | body
    return door.post(REPORT, json=whole, headers=sent(key, origin))


def answered(door, conn, data_dir, job=None):
    """A fill that has had its answers; its ticket."""
    ticket = running(door, conn, data_dir, job)
    assert send(door, ticket, [NAME_BOX]).json()["go"]
    return ticket


def reported(conn):
    return one(conn, "SELECT state, stop_kind, changes, filled FROM fills ORDER BY id DESC")


def ok(response):
    return (response.status_code, response.json()) == (200, {"ok": True})


STOPS = ("bot-check", "limit", "unexpected-page", "cant-set")
LEFT_WORDS = {
    "no-box": "the plugin couldn't find this box",
    "custom": "this drop-down is yours to pick",
    "place": "pick this place from the site's list yourself",
    "no-choice": "the plugin couldn't tell which choice is yours",
    "cant-set": "the form wouldn't let the plugin set this",
    "no-file": "the PDF couldn't be fetched",
    "file-refused": "the box doesn't show the PDF: check it",
    "didnt-stick": "the form shows something else here: check it",
    "not-reached": "the fill stopped before this one",
}
VALUE = {"key": "first_name", "value": "Fake Person"}
LEFT = {"key": "first_name", "why": "no-box"}
BAD_KEYS = (
    {"key": None}, {"key": 1}, {"key": True}, {"key": ["first_name"]}, {"key": {"a": 1}}, {"key": "x" * 1001},
)  # fmt: skip
BAD_VALUES = (None, 1, True, 1.5, {"a": 1}, [1], [None], ["ok", 1], [["nested"]], "x" * 20_001, ["ok", "x" * 20_001])
BAD_LISTS = (None, 1, True, "x", {"key": "first_name"}, [1], [None], ["first_name"], [[]])
BAD_REPORTS = (
    *({"state": state} for state in (None, 1, True, "", "FILLED", "sent", ["filled"], {"a": 1})),
    *(
        {"state": "stopped", "kind": kind}
        for kind in (None, 1, True, "", "other", "too-many-fields", "nothing-to-fill", ["limit"], {"a": 1})
    ),
    *({"kind": kind} for kind in (*STOPS, "other", "", 0, False, ["limit"])),
    *({"values": values} for values in BAD_LISTS),
    *({"left": left} for left in BAD_LISTS),
    *({"values": [VALUE | key]} for key in BAD_KEYS),
    *({"left": [LEFT | key]} for key in BAD_KEYS),
    *({"values": [VALUE | {"value": value}]} for value in BAD_VALUES),
    *({"left": [LEFT | {"why": why}]} for why in (None, 1, True, "", "other", ["no-box"], {"a": 1})),
    {"values": [{"key": "first_name"}]},
    {"values": [{"value": "Fake Person"}]},
    {"left": [{"key": "first_name"}]},
    {"left": [{"why": "no-box"}]},
    {"values": [VALUE] * 501},
    {"left": [LEFT] * 501},
    {"values": [VALUE, None]},  # one bad line refuses the good one with it
    {"left": [LEFT, None]},
)


def test_a_filled_report_keeps_the_questions_and_the_ticket(door, conn, data_dir, paired):
    ticket = answered(door, conn, data_dir)
    assert ok(report(door, ticket, values=[VALUE]))
    assert reported(conn) == ("filled", None, "[]", '["First Name"]')
    assert door.app.state.tickets.running(ticket, paired).reported


def test_a_filled_report_of_nothing_saves_an_empty_list(door, conn, data_dir, paired):
    assert ok(report(door, answered(door, conn, data_dir)))
    assert reported(conn) == ("filled", None, "[]", "[]")


@pytest.mark.parametrize(("why", "words"), LEFT_WORDS.items())
def test_each_line_left_goes_on_the_row_in_the_app_s_own_words(door, conn, data_dir, paired, why, words):
    assert ok(report(door, answered(door, conn, data_dir), left=[{"key": "first_name", "why": why}]))
    assert changes(conn) == [{"question": "First Name", "what": words}]
    assert reported(conn)[3] == "[]"


def test_lines_left_come_before_what_the_compare_said(door, conn, data_dir, paired):
    ticket = running(door, conn, data_dir)
    extra = {"key": "question_1", "label": "Why Acme?", "kind": "long_text", "required": False}
    assert send(door, ticket, [NAME_BOX, extra]).json()["go"]
    assert ok(report(door, ticket, left=[LEFT]))
    assert changes(conn) == [
        {"question": "First Name", "what": LEFT_WORDS["no-box"]},
        {"question": 'The form shows "Why Acme?"', "what": "new on the form"},
    ]


@pytest.mark.parametrize(
    ("extras", "last"),
    [
        (99, {"question": 'The form shows "x"', "what": "new on the form"}),
        (100, {"question": "1 more", "what": "not shown here"}),
        (150, {"question": "51 more", "what": "not shown here"}),
    ],
)
def test_lines_left_keep_the_limit_of_100_and_its_count(door, conn, data_dir, paired, extras, last):
    ticket = running(door, conn, data_dir)
    others = [{"key": f"k{number}", "label": "x", "kind": "text", "required": False} for number in range(extras)]
    assert send(door, ticket, [NAME_BOX, *others]).json()["go"]
    assert ok(report(door, ticket, left=[LEFT]))
    assert len(changes(conn)) == (100 if extras == 99 else 101) and changes(conn)[-1] == last
    assert changes(conn)[0] == {"question": "First Name", "what": LEFT_WORDS["no-box"]}  # never lost in the count


@pytest.mark.parametrize("body", [{"values": [VALUE] * 3}, {"left": [LEFT] * 3}])
def test_a_repeated_key_counts_once(door, conn, data_dir, paired, body):
    assert ok(report(door, answered(door, conn, data_dir), **body))
    assert len(json.loads(reported(conn)[3])) + len(changes(conn)) == 1


def test_a_line_that_didn_t_stick_is_only_for_you_to_check(owner, door, conn, data_dir, paired):
    job = ready_job(conn)
    ticket = answered(door, conn, data_dir, job)
    assert ok(report(door, ticket, values=[VALUE], left=[LEFT | {"why": "didnt-stick"}]))
    assert reported(conn)[3] == "[]"
    assert changes(conn) == [{"question": "First Name", "what": LEFT_WORDS["didnt-stick"]}]
    page = owner.get(f"/jobs/{job}/form").text
    assert "<h3>Filled</h3>" not in page and "<h3>Check these on the form</h3>" in page


def test_a_page_s_key_and_a_made_up_question_reach_nothing(owner, door, conn, data_dir, paired):
    job = ready_job(conn)
    ticket = running(door, conn, data_dir, job)
    drawn = {"key": "question_1", "label": "Drawn by the page", "kind": "text", "required": False}
    assert send(door, ticket, [NAME_BOX | {"label": "First Name *"}, drawn]).json()["go"]
    before = reported(conn)[2]
    made_up = {"question": "Made-up question", "label": "Made-up question"}
    assert ok(
        report(
            door, ticket,
            values=[VALUE | made_up, {"key": "question_1", "value": "x"}, {"key": "not-in-the-pack", "value": "x"}],
            left=[{"key": "question_1", "why": "custom"}, {"key": "Made-up key", "why": "no-box"}],
            question="Made-up question",
        )
    )  # fmt: skip
    assert reported(conn) == ("filled", None, before, '["First Name"]')
    pages = owner.get(f"/jobs/{job}/form").text + owner.get(SETTINGS).text
    assert "Made-up" not in pages and "not-in-the-pack" not in pages and "<li>First Name</li>" in pages


def test_with_no_answers_out_no_key_counts_on_a_stop(door, conn, data_dir, paired):
    ticket = running(door, conn, data_dir)
    assert ok(report(door, ticket, "stopped", kind="bot-check", values=[VALUE], left=[LEFT]))
    assert reported(conn) == ("stopped", "bot-check", "[]", "[]")


@pytest.mark.parametrize("ticket", [None, 1, True, ["x"], {"a": 1}, "x" * 2001])
def test_a_report_s_ticket_must_be_a_string_of_at_most_2000_characters(door, conn, data_dir, paired, ticket):
    answered(door, conn, data_dir)
    assert refused(report(door, ticket), 400, "bad-request")
    assert reported(conn)[0] == "sent"


def test_a_made_up_ticket_is_no_ticket_whatever_the_report(door, conn, data_dir, paired):
    answered(door, conn, data_dir)
    assert refused(report(door, "made-up"), 404, "no-ticket")
    assert refused(report(door, "made-up", "reread", values=None), 404, "no-ticket")
    assert reported(conn)[0] == "sent"


def test_another_browser_s_ticket_is_no_ticket_for_a_report(door, conn, data_dir, paired):
    pair(conn, SECOND_KEY, "Second", plugin=OTHER_PLUGIN)
    ticket = answered(door, conn, data_dir)
    assert refused(report(door, ticket, key=SECOND_KEY, origin=OTHER_PLUGIN), 404, "no-ticket")
    assert reported(conn)[0] == "sent"


def test_a_ticket_not_yet_started_is_no_ticket_for_a_report(door, conn, data_dir, paired):
    press(door, conn, data_dir, ready_job(conn))
    (ticket,) = held(door).values()
    assert refused(report(door, ticket.id, "stopped", kind="limit"), 404, "no-ticket")
    assert fill_rows(conn) == [(0, None, "waiting", None)]


def test_a_ticket_past_its_time_is_no_ticket_for_a_report(door, conn, data_dir, clock, paired):
    ticket = answered(door, conn, data_dir)
    clock.now += fills.RUNNING_SECONDS + 1
    assert refused(report(door, ticket), 404, "no-ticket")
    assert reported(conn)[0] == "sent"


@pytest.mark.parametrize("body", BAD_REPORTS)
def test_a_bad_report_saves_nothing_and_uses_nothing_up(door, conn, data_dir, paired, body):
    ticket = answered(door, conn, data_dir)
    assert refused(report(door, ticket, **body), 400, "bad-request")
    assert reported(conn) == ("sent", None, "[]", None)
    assert ok(report(door, ticket, values=[VALUE]))


@pytest.mark.parametrize("name", ["state", "values", "left"])
def test_a_report_missing_a_part_is_a_bad_request(door, conn, data_dir, paired, name):
    ticket = answered(door, conn, data_dir)
    whole = {"protocol": 1, "ticket": ticket, "state": "filled", "values": [], "left": []}
    del whole[name]
    assert refused(door.post(REPORT, json=whole, headers=sent()), 400, "bad-request")
    assert reported(conn)[0] == "sent"


@pytest.mark.parametrize(
    "body",
    [
        {"values": [VALUE] * 500, "left": [LEFT] * 500},
        {"values": [VALUE | {"key": "x" * 1000}], "left": [LEFT | {"key": "x" * 1000}]},
        {"values": [VALUE | {"value": "x" * 20_000}]},
        {"values": [VALUE | {"value": ["x" * 20_000, "y"]}]},
        {"values": [VALUE | {"value": []}]},
        {"kind": None},
    ],
)
def test_a_report_at_the_limits_is_read(door, conn, data_dir, paired, body):
    assert ok(report(door, answered(door, conn, data_dir), **body))
    assert reported(conn)[0] == "filled"


def test_filled_before_the_answers_went_out_is_a_bad_request(door, conn, data_dir, paired):
    ticket = running(door, conn, data_dir)
    assert refused(report(door, ticket, values=[VALUE]), 400, "bad-request")
    assert fill_rows(conn) == [(0, "Laptop Chrome", "started", None)]
    assert send(door, ticket, [NAME_BOX]).json()["go"]  # nothing was used up


@pytest.mark.parametrize("second", [{}, {"state": "stopped", "kind": "limit"}, {"left": [LEFT]}])
def test_a_second_report_says_ok_and_changes_nothing(door, conn, data_dir, paired, second):
    ticket = answered(door, conn, data_dir)
    assert ok(report(door, ticket, values=[VALUE]))
    first = reported(conn)
    assert ok(report(door, ticket, **second))
    assert reported(conn) == first and len(held(door)) == 1


@pytest.mark.parametrize("kind", STOPS)
def test_a_stop_before_the_answers_went_out_ends_the_fill_as_stopped(door, conn, data_dir, paired, kind):
    ticket = running(door, conn, data_dir)
    assert ok(report(door, ticket, "stopped", kind=kind))
    assert fill_rows(conn) == [(0, "Laptop Chrome", "stopped", kind)]
    assert not held(door) and refused(send(door, ticket, [NAME_BOX]), 404, "no-ticket")


@pytest.mark.parametrize("kind", STOPS)
def test_a_stop_after_the_answers_went_out_stays_sent(door, conn, data_dir, paired, kind):
    ticket = answered(door, conn, data_dir)
    assert ok(report(door, ticket, "stopped", kind=kind, values=[VALUE]))
    assert fill_rows(conn) == [(0, "Laptop Chrome", "sent", kind)]
    assert reported(conn)[3] == '["First Name"]' and not held(door)


def test_a_report_after_a_stop_finds_no_ticket_and_changes_nothing(door, conn, data_dir, paired):
    ticket = answered(door, conn, data_dir)
    assert ok(report(door, ticket, "stopped", kind="limit"))
    first = reported(conn)
    assert refused(report(door, ticket, "stopped", kind="bot-check"), 404, "no-ticket")
    assert reported(conn) == first


@pytest.mark.parametrize("body", [{}, {"state": "stopped", "kind": "limit"}])
def test_a_busy_database_changes_nothing_at_a_report(door, conn, data_dir, paired, body):
    ticket = answered(door, conn, data_dir)
    conn.execute("BEGIN IMMEDIATE")
    response = report(door, ticket, **{"values": [VALUE], "left": [LEFT]} | body)
    conn.rollback()
    assert refused(response, 503, "app-error")
    assert reported(conn) == ("sent", None, "[]", None) and len(held(door)) == 1
    assert ok(report(door, ticket, **{"values": [VALUE], "left": [LEFT]} | body))  # the report can be sent again


def test_a_report_needs_no_live_login(owner, door, conn, data_dir, paired):
    ticket = answered(door, conn, data_dir)
    log_out(owner)
    assert ok(report(door, ticket, values=[VALUE]))
    assert reported(conn)[0] == "filled"


def test_a_report_s_values_are_left_nowhere(owner, door, conn, data_dir, paired, caplog):
    fake_value = "fake-answer-never-kept"
    caplog.set_level(logging.DEBUG)
    job = ready_job(conn)
    ticket = answered(door, conn, data_dir, job)
    kept = next(iter(held(door).values()))
    with_fake_value = [VALUE | {"value": fake_value}, VALUE | {"value": [fake_value, "x"]}]
    answers = [
        report(door, ticket, values=[*with_fake_value, {"key": 1, "value": fake_value}]),  # refused
        report(door, ticket, values=with_fake_value, left=[LEFT]),
        report(door, ticket, values=with_fake_value),  # a repeat
    ]
    places = {
        "the answers": " ".join(answer.text for answer in answers),
        "the row": repr(conn.execute("SELECT * FROM fills").fetchall()),
        "the log": " ".join(record.getMessage() for record in caplog.records),
        "the ticket": repr(kept),
        "the box": owner.get(f"/jobs/{job}/form").text,
        "Settings": owner.get(SETTINGS).text,
    }
    assert ok(answers[1]) and kept.reported
    assert [place for place, text in places.items() if fake_value in text] == []  # names the place, never the value


# /plugin/file


def paper(conn, data_dir, job, kind="resume", version=1, body=RESUME_PDF):
    """A finished version of a job's resume or letter, with its PDF; where that is."""
    with conn:
        conn.execute(DONE, (job, kind, version))
    target = pdf.tailored(data_dir, job, version, kind)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    return target


def packed(door, conn, data_dir, letter=False, body=RESUME_PDF):
    """A fill that has had its answers, with a resume and, if asked, a letter in the pack; its job, ticket and PDF."""
    with conn:
        job = add_job(conn, "ML Intern", apply_url=LINK)
    submit.save(conn, job, Form((FIRST_NAME, RESUME_FIELD, LETTER_FIELD)), "api")
    preview.keep(conn, job, FIRST_NAME, "Fake")
    target = paper(conn, data_dir, job, body=body)
    if letter:
        paper(conn, data_dir, job, "cover_letter", body=LETTER_PDF)
    ticket = running(door, conn, data_dir, job)
    pack = send(door, ticket, [NAME_BOX, RESUME_BOX, LETTER_BOX]).json()["pack"]
    assert [line["value"] for line in pack if line["kind"] == "file"] == [
        {"slot": slot} for slot in ("resume", "cover_letter")[: 1 + letter]
    ]
    return job, ticket, target


def fetch(door, ticket, slot="resume", key=KEY, origin=PLUGIN):
    return door.post(PAPER, json={"protocol": 1, "ticket": ticket, "slot": slot}, headers=sent(key, origin))


def handed_out(conn):
    return one(conn, "SELECT files FROM fills ORDER BY id DESC")[0]


def goes_out(response, name, body):
    return (response.status_code, response.json()) == (200, {"name": name, "data": base64.b64encode(body).decode()})


def test_a_pdf_the_pack_named_goes_out_once_with_its_trail(door, conn, data_dir, paired):
    _, ticket, _ = packed(door, conn, data_dir)
    assert handed_out(conn) is None
    assert goes_out(fetch(door, ticket), "Resume.pdf", RESUME_PDF)
    assert handed_out(conn) == '["resume"]'
    assert refused(fetch(door, ticket), 404, "no-ticket")
    assert handed_out(conn) == '["resume"]'


def test_two_slots_each_go_out_once(door, conn, data_dir, paired):
    _, ticket, _ = packed(door, conn, data_dir, letter=True)
    assert goes_out(fetch(door, ticket, "cover_letter"), "Cover-letter.pdf", LETTER_PDF)
    assert goes_out(fetch(door, ticket), "Resume.pdf", RESUME_PDF)
    assert handed_out(conn) == '["cover_letter", "resume"]'
    assert refused(fetch(door, ticket, "cover_letter"), 404, "no-ticket")
    assert refused(fetch(door, ticket), 404, "no-ticket")


@pytest.mark.parametrize("slot", ["cover_letter", "made-up", "", "../resume", None, 1, True, ["resume"], {"a": 1}])
def test_a_slot_the_pack_didn_t_name_is_no_ticket(door, conn, data_dir, paired, slot):
    _, ticket, target = packed(door, conn, data_dir)
    paper(conn, data_dir, one(conn, "SELECT id FROM jobs")[0], "cover_letter", body=LETTER_PDF)  # made after the pack
    assert refused(fetch(door, ticket, slot), 404, "no-ticket")
    assert handed_out(conn) is None
    assert goes_out(fetch(door, ticket), "Resume.pdf", target.read_bytes())


def test_no_slot_in_the_body_is_no_ticket(door, conn, data_dir, paired):
    _, ticket, _ = packed(door, conn, data_dir)
    assert refused(door.post(PAPER, json={"protocol": 1, "ticket": ticket}, headers=sent()), 404, "no-ticket")


def test_a_made_up_ticket_gets_no_pdf(door, conn, data_dir, paired):
    packed(door, conn, data_dir)
    assert refused(fetch(door, "made-up"), 404, "no-ticket")


@pytest.mark.parametrize("ticket", [None, 1, True, ["x"], {"a": 1}, "x" * 2001])
def test_a_pdf_s_ticket_must_be_a_string_of_at_most_2000_characters(door, conn, data_dir, paired, ticket):
    packed(door, conn, data_dir)
    assert refused(fetch(door, ticket), 400, "bad-request")


def test_another_browser_s_ticket_gets_no_pdf(door, conn, data_dir, paired):
    pair(conn, SECOND_KEY, "Second", plugin=OTHER_PLUGIN)
    _, ticket, _ = packed(door, conn, data_dir)
    assert refused(fetch(door, ticket, key=SECOND_KEY, origin=OTHER_PLUGIN), 404, "no-ticket")
    assert handed_out(conn) is None and goes_out(fetch(door, ticket), "Resume.pdf", RESUME_PDF)


def test_a_ticket_with_no_answers_yet_gets_no_pdf(door, conn, data_dir, paired):
    with conn:
        job = add_job(conn, "ML Intern", apply_url=LINK)
    submit.save(conn, job, Form((FIRST_NAME, RESUME_FIELD)), "api")
    preview.keep(conn, job, FIRST_NAME, "Fake")
    paper(conn, data_dir, job)
    assert refused(fetch(door, running(door, conn, data_dir, job)), 404, "no-ticket")


def test_no_live_login_gets_no_pdf_and_uses_nothing_up(owner, door, conn, data_dir, paired):
    _, ticket, _ = packed(door, conn, data_dir)
    log_out(owner)
    assert refused(fetch(door, ticket), 403, "not-logged-in")
    assert handed_out(conn) is None
    log_in(owner)
    assert goes_out(fetch(door, ticket), "Resume.pdf", RESUME_PDF)


def test_a_job_with_its_record_gets_no_pdf_and_ends_the_fill(door, conn, data_dir, paired):
    job, ticket, _ = packed(door, conn, data_dir)
    with conn:
        conn.execute(
            "INSERT INTO submissions (job_id, submitted_on, saved_on, how, address, fields, files) "
            "VALUES (?, '2026-11-05', 'then', 'by hand', ?, '[]', '[]')",
            (job, FORM_ADDRESS),
        )
    assert refused(fetch(door, ticket), 404, "no-ticket")
    assert handed_out(conn) is None and not held(door)
    assert fill_rows(conn) == [(0, "Laptop Chrome", "sent", "submitted")]


def outside_the_folder(target, data_dir):
    copy = data_dir / "elsewhere.pdf"
    copy.write_bytes(RESUME_PDF)
    target.unlink()
    target.symlink_to(copy)


def in_another_job_s_folder(target, data_dir):
    copy = data_dir / "documents" / "999" / target.name
    copy.parent.mkdir()
    copy.write_bytes(RESUME_PDF)
    target.unlink()
    target.symlink_to(copy)


def changed_after_the_pack(target, data_dir):
    target.write_bytes(b"%PDF-1.7 rebuilt since")


def gone(target, data_dir):
    target.unlink()


def a_folder_in_its_place(target, data_dir):
    target.unlink()
    target.mkdir()


def a_link_to_itself(target, data_dir):
    target.unlink()
    target.symlink_to(target)


@pytest.mark.parametrize(
    "spoil",
    [
        outside_the_folder,
        in_another_job_s_folder,
        changed_after_the_pack,
        gone,
        a_folder_in_its_place,
        a_link_to_itself,
    ],
)
def test_a_spoiled_file_is_no_file_and_can_be_fetched_once_put_back(door, conn, data_dir, paired, spoil):
    _, ticket, target = packed(door, conn, data_dir)
    spoil(target, data_dir)
    assert refused(fetch(door, ticket), 404, "no-file")
    assert handed_out(conn) is None
    if target.is_dir() and not target.is_symlink():
        target.rmdir()
    else:
        target.unlink(missing_ok=True)
    target.write_bytes(RESUME_PDF)
    assert goes_out(fetch(door, ticket), "Resume.pdf", RESUME_PDF)


def test_a_job_s_folder_that_leads_elsewhere_is_no_file(door, conn, data_dir, paired):
    _, ticket, target = packed(door, conn, data_dir)
    moved = data_dir / "elsewhere"
    target.parent.rename(moved)
    target.parent.symlink_to(moved)
    assert refused(fetch(door, ticket), 404, "no-file")


@pytest.mark.parametrize(
    "body",
    [b"not a pdf", b"", b" %PDF-1.7", b"%PDF", b"%PDF 1.7", b"%PDF-" + bytes(10 * 2**20 - 4)],
    ids=["text", "empty", "a space first", "too short", "no dash", "a byte over 10 MB"],
)
def test_what_isn_t_a_pdf_of_at_most_10_mb_is_no_file(door, conn, data_dir, paired, body):
    _, ticket, _ = packed(door, conn, data_dir, body=body)
    assert refused(fetch(door, ticket), 404, "no-file")
    assert handed_out(conn) is None


def test_a_pdf_of_exactly_10_mb_goes_out(door, conn, data_dir, paired):
    body = b"%PDF-" + bytes(10 * 2**20 - 5)
    _, ticket, _ = packed(door, conn, data_dir, body=body)
    assert goes_out(fetch(door, ticket), "Resume.pdf", body)


def test_a_file_over_10_mb_is_never_read(door, conn, data_dir, paired, monkeypatch):
    _, ticket, target = packed(door, conn, data_dir)
    with target.open("ab") as file:
        file.truncate(11 * 2**20)
    read, reader = [], Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda path: read.append(path.name) or reader(path))
    assert refused(fetch(door, ticket), 404, "no-file")
    assert target.name not in read


def test_a_named_pipe_is_no_file_and_is_never_read(door, conn, data_dir, paired, monkeypatch):
    _, ticket, target = packed(door, conn, data_dir)
    target.unlink()
    os.mkfifo(target)
    read = []  # a read of a pipe would wait for ever, so this notes it and reads nothing: a slip fails, never hangs
    monkeypatch.setattr(Path, "read_bytes", lambda path: read.append(path.name) or b"")
    assert refused(fetch(door, ticket), 404, "no-file")
    assert target.name not in read


def test_a_newer_version_made_during_the_fill_never_goes_out(door, conn, data_dir, paired):
    job, ticket, _ = packed(door, conn, data_dir)
    paper(conn, data_dir, job, version=2, body=b"%PDF-1.7 a newer resume")
    assert goes_out(fetch(door, ticket), "Resume.pdf", RESUME_PDF)


def test_a_busy_database_sends_no_pdf_and_uses_nothing_up(door, conn, data_dir, paired):
    _, ticket, _ = packed(door, conn, data_dir)
    conn.execute("BEGIN IMMEDIATE")
    response = fetch(door, ticket)
    conn.rollback()
    assert refused(response, 503, "app-error")
    assert handed_out(conn) is None
    assert goes_out(fetch(door, ticket), "Resume.pdf", RESUME_PDF)


def test_no_answer_holds_a_path_and_no_log_holds_a_pdf(owner, door, conn, data_dir, paired, caplog):
    caplog.set_level(logging.DEBUG)
    _, ticket, target = packed(door, conn, data_dir)
    answers = [fetch(door, ticket, "made-up"), fetch(door, "made-up")]
    moved = target.with_suffix(".moved")
    target.rename(moved)
    answers.append(fetch(door, ticket))
    moved.rename(target)
    answers += [fetch(door, ticket), fetch(door, ticket)]
    assert [answer.status_code for answer in answers] == [404, 404, 404, 200, 404]
    paths = [word for answer in answers for word in (str(data_dir), target.name, "documents") if word in answer.text]
    logged = " ".join(record.getMessage() for record in caplog.records)
    pdf_words = (RESUME_PDF.decode(), base64.b64encode(RESUME_PDF).decode(), target.name)
    assert paths == [] and [word for word in pdf_words if word in logged] == []


def test_the_trail_outlives_a_report_and_remove(owner, door, conn, data_dir, paired):
    job, ticket, _ = packed(door, conn, data_dir)
    fetch(door, ticket)
    assert ok(report(door, ticket, values=[VALUE]))
    assert handed_out(conn) == '["resume"]'
    owner.post(REMOVE, data={"id": str(paired), "csrf_token": csrf(owner)})
    assert handed_out(conn) == '["resume"]' and fill_words.latest(conn, job)["files"] == "resume"


def test_a_trail_survives_a_stop_too(door, conn, data_dir, paired):
    _, ticket, _ = packed(door, conn, data_dir)
    fetch(door, ticket)
    assert ok(report(door, ticket, "stopped", kind="bot-check"))
    assert handed_out(conn) == '["resume"]'


@pytest.fixture
def refusals(client, door, conn):
    """One answer of each kind the door can give."""
    pair(conn, plugin=PLUGIN)
    answers = {
        "no key": door.post(HELLO, json=GOOD),
        "wrong plugin": door.post(HELLO, json=GOOD, headers=sent(origin=OTHER_PLUGIN)),
        "not found": door.get("/plugin/nothing-here", headers=sent()),
        "wrong method": door.get(HELLO, headers=sent()),
        "no such method": door.request("OPTIONS", HELLO, headers=sent()),
        "a page's preflight": door.options(
            HELLO, headers={"Origin": JOB_PAGE, "Access-Control-Request-Method": "POST",
                            "Access-Control-Request-Headers": "authorization,content-type"},
        ),
        "too big": door.post(HELLO, content=b"x" * (2**20 + 1), headers=sent()),
        "bad body": door.post(HELLO, content=b"{", headers=sent()),
        "old plugin": door.post(HELLO, json=GOOD | {"protocol": 2}, headers=sent()),
        "not logged in": call(door, KNOWN),
        "not logged in to fill": call(door, FILL),
        "a stranger's Host": door.post(HELLO, json=GOOD, headers=sent() | {"Host": "attacker.example"}),
        "welcome": door.post(HELLO, json=GOOD, headers=sent()),
    }  # fmt: skip
    log_in(client)
    return answers | {"no ticket": call(door, START)}


def test_the_kinds_of_refusal(refusals):
    assert refused(refusals["not found"], 404, "bad-request")
    assert refused(refusals["wrong method"], 405, "bad-request")
    assert refused(refusals["no such method"], 405, "bad-request")
    assert refused(refusals["a page's preflight"], 401, "no-key")
    assert refused(refusals["too big"], 413, "too-big")
    assert refused(refusals["a stranger's Host"], 400, "bad-request")
    assert refused(refusals["no ticket"], 404, "no-ticket")
    assert refused(refusals["not logged in"], 403, "not-logged-in")
    assert refused(refusals["not logged in to fill"], 403, "not-logged-in")


def test_no_answer_at_the_door_sets_a_cookie_or_opens_cors(refusals):
    for response in refusals.values():
        assert "set-cookie" not in response.headers
        assert not [name for name in response.headers if name.lower().startswith("access-control-")]


def test_every_answer_at_the_door_is_json_with_the_safety_headers(refusals):
    for response in refusals.values():
        assert response.headers["content-type"] == "application/json"
        for name, value in security.SAFETY_HEADERS.items():
            assert response.headers[name] == value


def test_a_valid_login_gets_no_cookie_at_the_door_either(owner, conn):
    pair(conn, plugin=PLUGIN)
    assert "set-cookie" not in owner.post(HELLO, json=GOOD, headers=sent()).headers


def test_a_busy_database_is_503_as_json_and_saves_nothing(door, conn):
    pair(conn, plugin=PLUGIN)
    conn.execute("BEGIN IMMEDIATE")
    response = door.post(HELLO, json=GOOD, headers=sent())
    conn.rollback()
    assert refused(response, 503, "app-error")
    assert row(conn)[1:] == (None, None)


def test_a_busy_database_on_the_first_call_pins_nothing_and_can_be_tried_again(door, conn):
    pair(conn)
    conn.execute("BEGIN IMMEDIATE")
    response = door.post(HELLO, json=GOOD, headers=sent())
    conn.rollback()
    assert refused(response, 503, "app-error")
    assert row(conn) == (None, None, None)
    assert door.post(HELLO, json=GOOD, headers=sent()).status_code == 200


class MadeUpCrash(Exception):
    pass


def crash(error):
    def now():
        raise error

    return now


def test_a_crash_is_app_error_and_never_names_the_exception(door, conn, monkeypatch, caplog):
    pair(conn, plugin=PLUGIN)
    monkeypatch.setattr(collect, "now", crash(MadeUpCrash("person@example.com")))
    response = door.post(HELLO, json=GOOD, headers=sent())
    assert refused(response, 500, "app-error")
    assert "MadeUpCrash" not in response.text and "person@example.com" not in response.text
    assert "person@example.com" not in caplog.text


@pytest.mark.parametrize(
    ("status", "detail", "kind"),
    [
        (400, "Some page words about a job", "bad-request"),
        (403, "person@example.com", "bad-request"),
        (409, "no-such-kind", "bad-request"),
        (500, "Something broke", "app-error"),
        (503, "Busy words", "app-error"),
        (403, "not-logged-in", "not-logged-in"),  # on the agreement's list
    ],
)
def test_a_refusals_words_are_passed_on_only_if_they_are_a_kind(door, conn, monkeypatch, status, detail, kind):
    pair(conn, plugin=PLUGIN)
    monkeypatch.setattr(collect, "now", crash(HTTPException(status, detail)))
    assert refused(door.post(HELLO, json=GOOD, headers=sent()), status, kind)


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "attacker.example"},
        {"Host": "evil.ts.net"},
        {"Host": "127.0.0.1:8765"} | IDENTITY,
        {"Host": "127.0.0.1:8765"} | FORWARDED,
        {"Host": "127.0.0.1:8765"} | FUNNEL,
        {"Host": HOST},
        {"Host": HOST} | FUNNEL,
        {"Host": HOST} | IDENTITY | FORWARDED | FUNNEL,
    ],
)
def test_refused_addresses_stay_refused_even_with_the_key_and_answer_as_json(phone_dashboard, conn, headers):
    laptop, _ = phone_dashboard
    pair(conn, plugin=PLUGIN)
    response = laptop.post(HELLO, json=GOOD, headers=sent() | headers)
    assert response.status_code in (400, 403)
    assert response.json() == {"error": "bad-request"}
    assert row(conn)[1:] == (None, None)


def test_the_address_checks_come_before_the_key(phone_dashboard):
    laptop, _ = phone_dashboard
    assert refused(laptop.post(HELLO, headers={"Host": "attacker.example"}), 400, "bad-request")
    assert refused(laptop.post(HELLO, headers=FUNNEL), 403, "bad-request")


def test_the_door_works_the_same_through_the_phone_address(phone_dashboard, conn):
    _, phone = phone_dashboard
    pair(conn)
    assert refused(phone.post(HELLO, json=GOOD), 401, "no-key")
    assert refused(phone.post(HELLO, json=GOOD, headers=sent(origin=PHONE)), 403, "wrong-plugin")
    assert phone.post(HELLO, json=GOOD, headers=sent()).status_code == 200


# Settings → Browser plugin


def add(owner, name="Laptop Chrome"):
    return owner.post(SETTINGS, data={"name": name, "csrf_token": csrf(owner)})


def test_settings_links_to_the_page(owner, tmp_path, data_dir, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(f"data_dir: {data_dir}\n", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", path)  # the whole app reads this file
    assert f'href="{SETTINGS}"' in owner.get("/settings").text


def test_the_pairing_pages_need_a_login(client, conn):
    pair(conn)
    assert client.get(SETTINGS, follow_redirects=False).headers["location"] == "/login"
    for path in (SETTINGS, REMOVE):
        response = client.post(path, data={"name": "New", "id": "1"}, follow_redirects=False)
        assert response.headers["location"] == "/login"
    assert count(conn) == 1


def test_the_pairing_pages_need_the_anti_forgery_token(owner, conn):
    pair(conn)
    assert owner.post(SETTINGS, data={"name": "New"}).status_code == 403
    assert owner.post(REMOVE, data={"id": "1"}).status_code == 403
    token = {"csrf_token": csrf(owner)}
    sneaked = {"Origin": "https://jobs.example"}
    assert owner.post(SETTINGS, data={"name": "New"} | token, headers=sneaked).status_code == 403
    assert count(conn) == 1


def test_adding_shows_the_key_once_and_keeps_only_its_hash(owner, door, conn):
    response = add(owner)
    key = SHOWN_KEY.search(response.text)[1]
    assert len(key) >= 43  # 256 random bits
    assert "copy it now, it won't be shown again" in words(response)
    assert response.headers["cache-control"] == "no-store"
    name, stored = one(conn, "SELECT name, key_hash FROM paired_browsers")
    assert (name, stored) == ("Laptop Chrome", hashed(key))
    assert key not in owner.get(SETTINGS).text
    assert door.post(HELLO, json=GOOD, headers=sent(key)).status_code == 200


def test_each_browser_gets_its_own_key(owner, conn):
    first, second = (SHOWN_KEY.search(add(owner, name).text)[1] for name in ("First", "Second"))
    assert first != second and count(conn) == 2


def test_the_key_is_never_logged(owner, door, caplog):
    caplog.set_level(logging.DEBUG)
    key = SHOWN_KEY.search(add(owner).text)[1]
    door.post(HELLO, json=GOOD, headers=sent(key))
    door.post(HELLO, json=GOOD, headers=sent(key, JOB_PAGE))
    assert key not in caplog.text and hashed(key) not in caplog.text


def test_a_reload_of_add_is_refused_and_makes_no_second_row(owner, conn):
    add(owner)
    response = add(owner)
    assert (response.status_code, NAME_TAKEN in words(response)) == (400, True)
    assert not SHOWN_KEY.search(response.text)
    assert count(conn) == 1


@pytest.mark.parametrize("name", ["", "   ", "x" * 41, "tab\there", "a\nb", "nul\x00", "bell\x07", "del\x7f", "\x85"])
def test_a_bad_name_is_refused_with_what_to_do(owner, conn, name):
    response = add(owner, name)
    assert (response.status_code, BAD_NAME in words(response)) == (400, True)
    assert count(conn) == 0


@pytest.mark.parametrize("name", ["x" * 40, "  Work laptop  ", "Büro Chrome", "ノートPC", "متصفحي", 'O\'Neil "home"'])
def test_odd_but_fair_names_are_kept_and_shown_safely(owner, conn, name):
    assert add(owner, name).status_code == 200
    assert one(conn, "SELECT name FROM paired_browsers") == (name.strip(),)
    assert name.strip() in words(owner.get(SETTINGS))


def test_a_name_is_escaped_where_it_is_shown(owner):
    add(owner, "<script>alert(1)</script>")
    text = owner.get(SETTINGS).text
    assert "&lt;script&gt;alert(1)" in text and "<script>alert(1)" not in text


def test_the_list_shows_what_is_known_of_each_browser(owner, door, conn):
    pair(conn)
    text = words(owner.get(SETTINGS))
    assert "Laptop Chrome" in text and "2026-11-05" in text and "No plugin has connected yet" in text
    assert WRONG_PLUGIN_HINT in text
    door.post(HELLO, json=GOOD | {"version": "9.8.7"}, headers=sent())
    text = words(owner.get(SETTINGS))
    assert "9.8.7" in text and "No plugin has connected yet" not in text


def test_the_list_says_when_a_plugin_is_pinned_but_has_not_said_hello(owner, door, conn):
    pair(conn)
    door.post(HELLO, json=GOOD | {"version": "bad"}, headers=sent())
    text = words(owner.get(SETTINGS))
    assert NOT_FINISHED in text and "No plugin has connected yet" not in text
    door.post(HELLO, json=GOOD, headers=sent())
    assert NOT_FINISHED not in words(owner.get(SETTINGS))


def test_the_list_shows_each_plugin_id_to_compare_and_nothing_for_a_browser_with_none(owner, door, conn):
    pair(conn)
    pair(conn, "second-fake-key", "Second")
    text = words(owner.get(SETTINGS))
    assert "Plugin ID" not in text and COMPARE_HINT not in text
    door.post(HELLO, json=GOOD | {"version": "bad"}, headers=sent())
    for state in (NOT_FINISHED, "1.2.3"):
        text = words(owner.get(SETTINGS))
        assert state in text and SHOWN_ID in text and COMPARE_HINT in text
        assert text.count("Plugin ID") == 1 and "chrome-extension://" not in text
        door.post(HELLO, json=GOOD, headers=sent())


def test_the_phone_can_pair_a_browser_too(phone_dashboard, conn):
    _, phone = phone_dashboard
    cookie, session = phone_login(phone)
    data = {"name": "Phone added", "csrf_token": session.csrf}
    response = phone.post(SETTINGS, data=data, headers=cookie | ORIGIN)
    assert (response.status_code, count(conn)) == (200, 1)


def test_a_busy_database_adds_nothing_and_says_so(owner, conn):
    conn.execute("BEGIN IMMEDIATE")
    response = add(owner)
    conn.rollback()
    assert (response.status_code, BUSY in response.text) == (503, True)
    assert not SHOWN_KEY.search(response.text)
    assert count(conn) == 0


def test_remove_deletes_that_browser_only_and_its_key_stops_working(owner, door, conn):
    first = pair(conn, plugin=PLUGIN)
    pair(conn, "second-fake-key", "Second", plugin=PLUGIN)
    with conn:
        job = add_job(conn, "ML Intern")
    response = owner.post(REMOVE, data={"id": str(first), "csrf_token": csrf(owner)}, follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, SETTINGS)
    assert one(conn, "SELECT name FROM paired_browsers") == ("Second",)
    assert one(conn, "SELECT id FROM jobs WHERE id = ?", job) == (job,)
    assert refused(door.post(HELLO, json=GOOD, headers=sent()), 401, "no-key")
    assert door.post(HELLO, json=GOOD, headers=sent("second-fake-key")).status_code == 200


def two_browsers_with_fills(owner, door, conn, data_dir):
    """The first browser's running fill and toolbar fill, the second's toolbar fill, and a button fill nobody took."""
    first = pair(conn, plugin=PLUGIN)
    second = pair(conn, SECOND_KEY, "Second", plugin=OTHER_PLUGIN)
    addresses = [f"https://jobs.lever.co/acme/{number}/apply" for number in range(4)]
    jobs = [ready_job(conn, address.removesuffix("/apply"), f"Job {address[-7]}") for address in addresses]
    assert call(door, FILL, addresses[0]).status_code == 200
    assert call(door, START, addresses[0]).status_code == 200
    assert call(door, FILL, addresses[1]).status_code == 200
    door.post(FILL, json={"protocol": 1, "address": addresses[2]}, headers=sent(SECOND_KEY, OTHER_PLUGIN))
    press(door, conn, data_dir, jobs[3])
    return first, second, addresses


def test_remove_ends_that_browser_s_fills_and_leaves_the_rest(owner, door, conn, data_dir, limits):
    first, _, addresses = two_browsers_with_fills(owner, door, conn, data_dir)
    owner.post(REMOVE, data={"id": str(first), "csrf_token": csrf(owner)})
    assert fill_rows(conn) == [
        (1, "Laptop Chrome", "ended", "browser-removed"),
        (1, "Laptop Chrome", "ended", "browser-removed"),
        (1, "Second", "waiting", None),
        (0, None, "waiting", None),
    ]
    assert len(held(door)) == 2
    assert refused(call(door, START, addresses[0]), 401, "no-key")
    assert refused(call(door, FILL, addresses[1]), 401, "no-key")
    headers = sent(SECOND_KEY, OTHER_PLUGIN)
    assert door.post(START, json={"protocol": 1, "address": addresses[1]}, headers=headers).status_code == 404
    assert door.post(START, json={"protocol": 1, "address": addresses[2]}, headers=headers).status_code == 200
    assert door.post(START, json={"protocol": 1, "address": addresses[3]}, headers=headers).status_code == 200


def test_log_out_all_ends_every_fill(owner, door, conn, data_dir, limits):
    _, _, addresses = two_browsers_with_fills(owner, door, conn, data_dir)
    owner.post(LOG_OUT_ALL, data={"csrf_token": csrf(owner)}, follow_redirects=False)
    ended = [row_[2:] for row_ in fill_rows(conn)]
    assert ended == [("ended", "browser-removed")] * 3 + [("ended", "logged-out")]
    assert not held(door)
    assert refused(call(door, START, addresses[3]), 401, "no-key")
    assert refused(call(door, FILL, addresses[3]), 401, "no-key")
    pair(conn, "third-fake-key", "Third", plugin=PLUGIN)
    assert refused(call(door, START, addresses[3], "third-fake-key"), 403, "not-logged-in")
    assert refused(call(door, FILL, addresses[3], "third-fake-key"), 403, "not-logged-in")
    log_in(owner)
    assert refused(call(door, START, addresses[3], "third-fake-key"), 404, "no-ticket")  # the old press is gone too


@pytest.mark.parametrize("browser_id", ["999", "not a number", "", "1.0", "9" * 40])
def test_a_bad_id_at_remove_ends_nothing(owner, door, conn, data_dir, limits, browser_id):
    two_browsers_with_fills(owner, door, conn, data_dir)
    response = owner.post(REMOVE, data={"id": browser_id, "csrf_token": csrf(owner)}, follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, SETTINGS)
    assert count(conn) == 2 and len(held(door)) == 4
    assert [row_[2] for row_ in fill_rows(conn)] == ["started", "waiting", "waiting", "waiting"]


def test_a_busy_database_at_remove_ends_nothing(owner, door, conn, data_dir, limits):
    first, _, _ = two_browsers_with_fills(owner, door, conn, data_dir)
    conn.execute("BEGIN IMMEDIATE")
    response = owner.post(REMOVE, data={"id": str(first), "csrf_token": csrf(owner)})
    conn.rollback()
    assert response.status_code == 503
    assert len(held(door)) == 4 and count(conn) == 2


def test_a_busy_database_at_log_out_all_ends_nothing(owner, door, conn, data_dir, limits):
    two_browsers_with_fills(owner, door, conn, data_dir)
    conn.execute("BEGIN IMMEDIATE")
    response = owner.post(LOG_OUT_ALL, data={"csrf_token": csrf(owner)}, follow_redirects=False)
    conn.rollback()
    assert response.status_code == 503
    assert len(held(door)) == 4 and count(conn) == 2


def test_a_new_browser_never_gets_a_removed_browsers_number(owner, conn):
    add(owner, "First")
    add(owner, "Second")
    old_press = {"id": "2", "csrf_token": csrf(owner)}
    owner.post(REMOVE, data=old_press)
    add(owner, "Third")
    assert conn.execute("SELECT id, name FROM paired_browsers ORDER BY id").fetchall() == [(1, "First"), (3, "Third")]
    owner.post(REMOVE, data=old_press)  # a second tab, still showing the old list
    assert count(conn) == 2


@pytest.mark.parametrize("browser_id", ["1", "999", "not a number", ""])
def test_removing_a_browser_already_gone_just_shows_the_list(owner, conn, browser_id):
    response = owner.post(REMOVE, data={"id": browser_id, "csrf_token": csrf(owner)}, follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, SETTINGS)


def test_a_busy_database_removes_nothing_and_says_so(owner, conn):
    pair(conn)
    conn.execute("BEGIN IMMEDIATE")
    response = owner.post(REMOVE, data={"id": "1", "csrf_token": csrf(owner)})
    conn.rollback()
    assert (response.status_code, BUSY in response.text) == (503, True)
    assert count(conn) == 1


# "Log out all devices"


def test_log_out_all_also_unpairs_every_browser_and_nothing_else(owner, door, conn):
    pair(conn, plugin=PLUGIN)
    pair(conn, "second-fake-key", "Second", plugin=PLUGIN)
    with conn:
        job = add_job(conn, "ML Intern")
    response = owner.post(LOG_OUT_ALL, data={"csrf_token": csrf(owner)}, follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/login")
    assert count(conn) == 0
    assert not owner.app.state.logins.sessions
    assert one(conn, "SELECT id FROM jobs WHERE id = ?", job) == (job,)
    assert refused(door.post(HELLO, json=GOOD, headers=sent()), 401, "no-key")


def test_a_busy_database_stops_log_out_all_before_anyone_is_logged_out(owner, conn):
    pair(conn)
    conn.execute("BEGIN IMMEDIATE")
    response = owner.post(LOG_OUT_ALL, data={"csrf_token": csrf(owner)}, follow_redirects=False)
    conn.rollback()
    assert (response.status_code, BUSY in response.text) == (503, True)
    assert owner.app.state.logins.sessions and count(conn) == 1
    assert owner.post(LOG_OUT_ALL, data={"csrf_token": csrf(owner)}, follow_redirects=False).status_code == 303
    assert count(conn) == 0


def test_the_log_out_all_words_say_browsers_go_too(phone_dashboard):
    laptop, _ = phone_dashboard
    log_in(laptop)
    assert LOG_OUT_ALL_HINT in words(laptop.get("/settings/phone"))


# Other paths: nothing changes


def test_other_paths_still_turn_away_another_sites_post(owner):
    response = owner.post("/settings/phone", data={"csrf_token": csrf(owner)}, headers={"Origin": PLUGIN})
    assert response.status_code == 403


def test_other_paths_still_answer_with_the_html_page(client):
    log_in(client)
    response = client.get("/no-such-page")
    assert (response.status_code, response.headers["content-type"]) == (404, "text/html; charset=utf-8")


def test_other_paths_are_not_affected_by_a_key_or_a_plugin_origin(client):
    assert client.get("/", headers=sent(), follow_redirects=False).headers["location"] == "/login"
    assert client.get("/login", headers={"Origin": PLUGIN}).status_code == 200


def test_a_crash_elsewhere_is_still_the_html_page(owner, monkeypatch):
    def crashes(*args):
        raise sqlcipher3.DatabaseError("made up")

    monkeypatch.setattr(main.queries, "list_jobs", crashes)
    response = owner.get("/")
    assert (response.status_code, response.headers["content-type"]) == (500, "text/html; charset=utf-8")
