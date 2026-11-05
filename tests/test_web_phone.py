"""The phone address: who may come in through Tailscale Serve, with fake headers. No account, no Tailscale, no phone."""

import html
import re
from contextlib import ExitStack

import httpx
import pytest
from fastapi.testclient import TestClient
from helpers import ADDRESS, PASSWORD, log_in

from app.config import Config
from app.fetch import Fetcher
from app.web import __main__ as web_command
from app.web import main, preparer, security

PHONE = "https://laptop.tail0000.ts.net"
HOST = "laptop.tail0000.ts.net"
LOCAL = "127.0.0.1:8765"
IDENTITY = {"Tailscale-User-Login": "person@example.com"}
FUNNEL = {"Tailscale-Funnel-Request": "?1"}
FORWARDED = {"X-Forwarded-For": "100.64.0.1", "X-Forwarded-Host": HOST, "X-Forwarded-Proto": "https"}
MARKS = [{name: value} for name, value in (IDENTITY | FORWARDED).items()]  # each of Serve's marks alone
PATHS = ("/", "/login", "/static/app.css", "/static/manifest.webmanifest")
# written out, so a change of words is a decision
NOT_ALLOWED = "This address is not allowed. Open the dashboard at the address shown in the terminal."
UNKNOWN_PHONE = (
    "This is a Tailscale address the dashboard doesn't know. On the laptop, add it in Settings → Phone, then "
    "restart the dashboard."
)
PUBLIC = "The dashboard can't be opened from the public internet. On the laptop, run: tailscale funnel reset"
NOT_TAILNET = (
    "Open this from a phone signed in to your Tailscale. On the laptop itself, use the address shown in the terminal."
)
EXPIRED = "This page has expired; reload it and try again."
REFUSED_ROUTES = [
    {"Host": HOST} | FUNNEL,
    {"Host": HOST} | IDENTITY | FORWARDED | FUNNEL,
    {"Host": LOCAL} | FUNNEL,
    {"Host": "attacker.example"} | FUNNEL,
    {"Host": ""} | FUNNEL,
    {"Host": HOST},
    {"Host": HOST, "Tailscale-User-Login": ""},
    *({"Host": LOCAL} | mark for mark in MARKS),
]


@pytest.fixture
def serve(data_dir, clock, web, monkeypatch):
    """Starts the dashboard with this phone address, the way the `client` fixture does."""
    monkeypatch.setattr(preparer.Preparer, "wake", lambda self: None)
    with ExitStack() as running:

        def start(address):
            logins = security.Logins(clock=clock)
            app = main.create_app(data_dir, logins, lambda: Fetcher(httpx.MockTransport(web)), phone_address=address)
            return running.enter_context(TestClient(app, base_url=ADDRESS))

        yield start


@pytest.fixture
def laptop(serve):
    """The laptop's own address, with the phone address set."""
    return serve(PHONE)


@pytest.fixture(params=[PHONE, ""], ids=["setting on", "setting empty"])
def either(serve, request):
    return serve(request.param)


@pytest.fixture
def phone(laptop):
    """Serve's real traffic: plain http to the app, with the phone's Host and Serve's headers. Not an https client, or
    code wrongly reading the scheme would pass. POSTs add ORIGIN, as a browser does."""
    client = TestClient(laptop.app, base_url=f"http://{HOST}", headers={"Host": HOST} | IDENTITY | FORWARDED)
    client.portal = laptop.portal  # the same running dashboard: its database belongs to that thread
    return client


ORIGIN = {"Origin": PHONE}


def tries(client, headers):
    """The same request on every way in, a login with the right password among them."""
    pages = [client.get(path, headers=headers, follow_redirects=False) for path in PATHS]
    return pages + [client.post("/login", data={"password": PASSWORD}, headers=headers, follow_redirects=False)]


def words(response):
    return html.unescape(response.text)


def test_the_phone_opens_login_and_the_program_files(phone):
    assert phone.get("/login").status_code == 200
    assert phone.get("/static/app.css").status_code == 200
    assert phone.get("/static/manifest.webmanifest").status_code == 200
    assert phone.get("/", follow_redirects=False).headers["location"] == "/login"


def test_the_public_internet_is_refused_whatever_the_host_says(either):
    for host in (HOST, LOCAL, "attacker.example", ""):
        for extra in ({}, IDENTITY, FORWARDED, {"Tailscale-Funnel-Request": ""}):
            for response in tries(either, {"Host": host} | FUNNEL | extra):
                assert (response.status_code, PUBLIC in words(response)) == (403, True)


def test_any_mark_of_serve_on_a_local_address_is_refused(either):
    for host in (LOCAL, "localhost:8765"):
        for mark in MARKS:
            for response in tries(either, {"Host": host} | mark):
                assert (response.status_code, NOT_TAILNET in words(response)) == (403, True)


def test_the_phone_address_needs_an_identity(laptop):
    for identity in ({}, {"Tailscale-User-Login": ""}):
        for response in tries(laptop, {"Host": HOST} | FORWARDED | identity):
            assert (response.status_code, NOT_TAILNET in words(response)) == (403, True)


def test_a_right_identity_under_a_local_host_is_not_let_in_as_the_laptop(laptop):
    headers = {"Host": LOCAL} | IDENTITY | FORWARDED
    assert {response.status_code for response in tries(laptop, headers)} == {403}
    assert not laptop.app.state.logins.sessions


def test_underscore_spellings_are_not_the_real_headers(laptop, phone):
    fake_identity = {"Host": HOST, "Tailscale_User_Login": "person@example.com"}
    assert laptop.get("/login", headers=fake_identity).status_code == 403
    assert phone.get("/login", headers={"Tailscale_Funnel_Request": "?1"}).status_code == 200
    plain = {"Tailscale_User_Login": "person@example.com", "Tailscale_Funnel_Request": "?1"}
    assert laptop.get("/login", headers=plain).status_code == 200


def test_refused_routes_spend_no_wrong_passwords_and_make_no_login(either):
    for headers in REFUSED_ROUTES:
        for _ in range(security.MAX_WRONG):
            assert log_in_with(either, "wrong guess", headers).status_code in (400, 403)
        assert log_in_with(either, PASSWORD, headers).status_code in (400, 403)
    logins = either.app.state.logins
    assert (logins.wrong, logins.locked_minutes(), logins.sessions) == (0, 0, {})
    assert log_in(either).status_code == 303


def log_in_with(client, password, headers):
    return client.post("/login", data={"password": password}, headers=headers, follow_redirects=False)


def test_only_the_exact_phone_address_counts(laptop):
    for host, sentence in (
        ("evil.ts.net", UNKNOWN_PHONE),
        (f"{HOST}:8443", UNKNOWN_PHONE),
        (HOST.upper(), UNKNOWN_PHONE),
        (f"{HOST}.evil.com", NOT_ALLOWED),
        (f"x{HOST}", UNKNOWN_PHONE),
        ("attacker.example:8765", NOT_ALLOWED),
        ("", NOT_ALLOWED),
    ):
        response = laptop.get("/login", headers={"Host": host} | IDENTITY | FORWARDED)
        assert (response.status_code, sentence in words(response)) == (400, True)


def test_a_right_forwarded_host_under_a_wrong_host_counts_for_nothing(laptop):
    assert laptop.get("/login", headers={"Host": LOCAL, "X-Forwarded-Host": HOST}).status_code == 403
    assert laptop.get("/login", headers={"Host": "attacker.example", "X-Forwarded-Host": HOST}).status_code == 400


def test_a_phone_address_with_a_port_counts_with_that_port_only(serve):
    dashboard = serve(f"{PHONE}:8443")
    assert dashboard.get("/login", headers={"Host": f"{HOST}:8443"} | IDENTITY).status_code == 200
    assert dashboard.get("/login", headers={"Host": HOST} | IDENTITY).status_code == 400
    sent = {"Host": f"{HOST}:8443", "Origin": f"{PHONE}:8443"} | IDENTITY
    assert log_in_with(dashboard, "wrong guess", sent).status_code == 401
    assert log_in_with(dashboard, "wrong guess", sent | {"Origin": PHONE}).status_code == 403


def test_a_form_must_come_from_the_address_it_was_sent_to(laptop, phone):
    for origin in (f"http://{HOST}", "https://attacker.example", f"{PHONE}:8443", "null"):
        response = log_in_with(phone, "wrong guess", {"Origin": origin})
        assert (response.status_code, EXPIRED in words(response)) == (403, True)
    response = log_in_with(laptop, "wrong guess", ORIGIN)
    assert (response.status_code, EXPIRED in words(response)) == (403, True)
    assert laptop.app.state.logins.wrong == 0


def test_a_form_from_the_phone_or_with_no_origin_passes(phone):
    for headers in (ORIGIN, {}):
        response = log_in_with(phone, "wrong guess", headers)
        assert (response.status_code, "Wrong password." in response.text) == (401, True)


def test_the_scheme_is_never_read(phone):
    plain = phone.get("/login")
    assert plain.url.scheme == "http"
    assert phone.get("/login", headers={"X-Forwarded-Proto": "http"}).text == plain.text
    assert log_in_with(phone, "wrong guess", ORIGIN | {"X-Forwarded-Proto": "http"}).status_code == 401
    assert "url.scheme" not in (main.HERE / "main.py").read_text()


def test_every_refusal_is_a_plain_page_with_nothing_private(laptop, phone):
    for response, status, sentence in (
        (laptop.get("/login", headers={"Host": "attacker.example"}), 400, NOT_ALLOWED),
        (laptop.get("/login", headers={"Host": "evil.ts.net"} | IDENTITY), 400, UNKNOWN_PHONE),
        (phone.get("/", headers=FUNNEL), 403, PUBLIC),
        (laptop.get("/", headers={"Host": HOST}), 403, NOT_TAILNET),
        (laptop.get("/", headers={"Host": LOCAL} | IDENTITY), 403, NOT_TAILNET),
        (log_in_with(phone, "wrong guess", {"Origin": "https://attacker.example"}), 403, EXPIRED),
    ):
        text = words(response)
        assert (response.status_code, sentence in text) == (status, True)
        assert response.headers["Cache-Control"] == "no-store"
        for private in ("person@example.com", "tail0000", "evil.ts.net", "Something went wrong", "csrf"):
            assert private not in text


def test_with_the_setting_empty_a_phone_gets_no_way_in(client):
    assert client.app.state.phone_host == ""
    response = client.get("/login", headers={"Host": "attacker.example"} | IDENTITY)
    assert (response.status_code, NOT_ALLOWED in words(response)) == (400, True)
    for response in tries(client, {"Host": HOST, "Origin": PHONE} | IDENTITY | FORWARDED):
        assert (response.status_code, UNKNOWN_PHONE in words(response)) == (400, True)


def test_the_laptop_is_as_today_with_the_setting_on_or_empty(either):
    assert either.get("/", follow_redirects=False).headers["location"] == "/login"
    assert log_in_with(either, "wrong guess", {"Origin": ADDRESS}).status_code == 401
    response = log_in(either)
    assert response.status_code == 303
    assert re.fullmatch(rf"{security.COOKIE}=[\w-]+; HttpOnly; Path=/; SameSite=strict", response.headers["Set-Cookie"])
    assert "<h1>Jobs</h1>" in either.get("/").text


@pytest.mark.parametrize(("setting", "host"), [(PHONE, HOST), ("", "")])
def test_the_start_command_passes_the_address_on_and_stays_on_this_computer(
    data_dir, monkeypatch, capsys, setting, host
):
    served = []
    monkeypatch.setattr(web_command, "load_config", lambda: Config(data_dir=data_dir, phone_address=setting))
    monkeypatch.setattr(web_command.uvicorn, "run", lambda app, **options: served.append((app, options)))
    monkeypatch.setattr(web_command.threading, "Timer", lambda *args: type("T", (), {"start": lambda self: None})())
    monkeypatch.setattr(web_command, "_port_free", lambda port: True)  # never ask what runs on this computer
    assert web_command.main([]) == 0
    app, options = served[0]
    assert app.state.phone_host == host
    assert (options["host"], options["proxy_headers"]) == ("127.0.0.1", False)
