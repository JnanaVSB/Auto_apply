"""The phone address: who may come in through Tailscale Serve, with fake headers. No account, no Tailscale, no phone."""

import html
import re
import stat
from contextlib import ExitStack
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, unquote, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from helpers import ADDRESS, PASSWORD, add_job, csrf, log_in
from starlette.exceptions import HTTPException
from test_gmail_client import FakeGoogle
from test_tracker import version
from test_web_emails import email
from test_web_tracker import APPLIED_CARD

from app import collect, config, tracker
from app.config import PROJECT_DIR, Config, load_config
from app.fetch import Fetcher
from app.gmail.client import CLIENT_ID, CLIENT_SECRET, REFRESH, SCOPE
from app.keychain import get_secret, set_secret
from app.web import __main__ as web_command
from app.web import gmail_settings, main, preparer, security

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
RESTART = "Saved. Restart the dashboard to use it."
SETTINGS = "/settings/phone"
CONNECT = "/settings/gmail/connect"
GMAIL_ON_THE_LAPTOP = "Connect Gmail on the laptop: Google sends you back to the laptop's own address."
LASTING = "made-up-lasting-sign-in"
LOG_OUT_ALL = "/settings/phone/log-out-all"
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


def test_the_login_check_reads_the_path_the_server_received(laptop, phone):
    for client in (laptop, phone):
        for path in ("/login%0a", "/static%0a/app.css"):
            response = client.get(path, follow_redirects=False)
            assert (response.status_code, response.headers.get("location")) == (303, "/login")
    made_up = {"Host": "127.0.0.1/login?"}  # a Host must not be able to change the path that is checked
    assert laptop.get("/", headers=made_up, follow_redirects=False).headers["location"] == "/login"


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


def phone_login(phone):
    """The cookie as a header, by hand: a real cookie jar won't send a Secure cookie over Serve's plain http."""
    cookie = log_in_with(phone, PASSWORD, ORIGIN).headers["Set-Cookie"].split(";")[0]
    return {"Cookie": cookie}, phone.app.state.logins.sessions[cookie.partition("=")[2]]


def test_the_phone_gets_a_cookie_only_https_and_this_exact_address_can_hold(phone):
    response = log_in_with(phone, PASSWORD, ORIGIN)
    assert (response.status_code, response.headers["location"]) == (303, "/")
    assert re.fullmatch(
        r"__Host-auto_apply_session=[\w-]+; HttpOnly; Path=/; SameSite=strict; Secure", response.headers["Set-Cookie"]
    )


def test_the_phones_cookie_opens_pages_and_a_change_needs_its_token(phone):
    cookie, session = phone_login(phone)
    page = phone.get("/", headers=cookie)
    assert (page.status_code, "<h1>Jobs</h1>" in page.text) == (200, True)
    assert page.headers["Cache-Control"] == "no-store"
    form = {"view": "table", "back": "/"}
    sent = phone.post(
        "/view", data=form | {"csrf_token": session.csrf}, headers=cookie | ORIGIN, follow_redirects=False
    )
    assert sent.status_code == 303
    refused = phone.post("/view", data=form, headers=cookie | ORIGIN, follow_redirects=False)
    assert (refused.status_code, EXPIRED in words(refused)) == (403, True)


def test_a_laptop_cookie_opens_nothing_on_the_phone_address(laptop, phone):
    log_in(laptop)
    token = laptop.cookies[security.COOKIE]
    cookie = {"Cookie": f"{security.COOKIE}={token}"}
    assert phone.get("/", headers=cookie, follow_redirects=False).headers["location"] == "/login"
    token_sent = {"X-CSRF-Token": csrf(laptop)} | cookie | ORIGIN
    assert phone.post("/logout", headers=token_sent, follow_redirects=False).headers["location"] == "/login"
    assert token in laptop.app.state.logins.sessions  # the phone's address can't end the laptop's login


def test_a_phone_cookie_opens_nothing_on_the_laptop_address(either):
    log_in(either)
    token = either.cookies[security.COOKIE]
    either.cookies.clear()
    cookie = {"Cookie": f"__Host-auto_apply_session={token}"}
    assert either.get("/", headers=cookie, follow_redirects=False).headers["location"] == "/login"


def test_logging_out_on_the_phone_ends_the_login_and_clears_the_cookie(phone):
    cookie, session = phone_login(phone)
    response = phone.post("/logout", headers=cookie | ORIGIN | {"X-CSRF-Token": session.csrf}, follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/login")
    assert re.fullmatch(
        r'__Host-auto_apply_session=""; expires=[^;]+; Max-Age=0; Path=/; SameSite=lax; Secure',
        response.headers["Set-Cookie"],
    )
    assert not phone.app.state.logins.sessions
    assert phone.get("/", headers=cookie, follow_redirects=False).headers["location"] == "/login"


def test_the_phone_is_logged_out_after_being_idle_like_the_laptop(phone, clock):
    cookie, _ = phone_login(phone)
    clock.now += security.IDLE_SECONDS - 60
    assert phone.get("/", headers=cookie).status_code == 200
    clock.now += security.IDLE_SECONDS + 1
    assert phone.get("/", headers=cookie, follow_redirects=False).headers["location"] == "/login"
    assert not phone.app.state.logins.sessions


def test_view_and_look_cookies_are_secure_on_the_phone_and_as_today_on_the_laptop(laptop, phone):
    view, look = {"view": "table", "back": "/"}, {"scheme": "E", "mode": "dark"}
    year = "HttpOnly; Max-Age=31536000; Path=/; SameSite=lax"
    cookie, session = phone_login(phone)
    token = {"csrf_token": session.csrf}
    sent = cookie | ORIGIN
    reply = phone.post("/view", data=view | token, headers=sent, follow_redirects=False)
    assert reply.headers["Set-Cookie"] == f"auto_apply_view=table; {year}; Secure"
    reply = phone.post("/look", data=look | token, headers=sent, follow_redirects=False)
    assert reply.headers["Set-Cookie"] == f"auto_apply_look=E.dark; {year}; Secure"
    log_in(laptop)
    token = {"csrf_token": csrf(laptop)}
    reply = laptop.post("/view", data=view | token, follow_redirects=False)
    assert reply.headers["Set-Cookie"] == f"auto_apply_view=table; {year}"
    reply = laptop.post("/look", data=look | token, follow_redirects=False)
    assert reply.headers["Set-Cookie"] == f"auto_apply_look=E.dark; {year}"


def test_the_laptops_logout_cookie_is_as_today(either):
    log_in(either)
    response = either.post("/logout", headers={"X-CSRF-Token": csrf(either)}, follow_redirects=False)
    assert re.fullmatch(
        rf'{security.COOKIE}=""; expires=[^;]+; Max-Age=0; Path=/; SameSite=lax', response.headers["Set-Cookie"]
    )


@pytest.fixture
def settings(tmp_path, data_dir, monkeypatch):
    """A temporary settings file with no phone address; `write` puts one in. Never the real data/config.yaml."""
    path = tmp_path / "config.yaml"
    path.write_text(f"data_dir: {data_dir}\n", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", path)  # the whole app reads this file
    return path


def write(settings, address):
    settings.write_text(f'{settings.read_text(encoding="utf-8")}phone_address: "{address}"\n', encoding="utf-8")


def save(client, typed):
    data = {"phone_address": typed, "csrf_token": csrf(client)}
    return client.post(SETTINGS, data=data, follow_redirects=False)


def box(response):
    """What the address box holds, as the page's own text."""
    return re.search(r'<input name="phone_address"[^>]* value="([^"]*)"', response.text)[1]


def test_the_phone_page_needs_a_login_and_a_save_needs_the_token(laptop, phone, settings):
    before = settings.read_bytes()
    for client in (laptop, phone):
        assert client.get(SETTINGS, follow_redirects=False).headers["location"] == "/login"
    data = {"phone_address": PHONE}
    assert laptop.post(SETTINGS, data=data, follow_redirects=False).headers["location"] == "/login"
    log_in(laptop)
    refused = laptop.post(SETTINGS, data=data, follow_redirects=False)
    assert (refused.status_code, EXPIRED in words(refused)) == (403, True)
    assert settings.read_bytes() == before


def test_the_laptop_saves_the_address_for_good_and_a_reload_sends_nothing_again(serve, settings):
    laptop = serve("")
    log_in(laptop)
    page = laptop.get(SETTINGS)
    assert (page.status_code, box(page)) == (200, "")
    assert "docs/phone-setup.md" in page.text and PHONE in page.text  # the guide, and the made-up example
    response = save(laptop, PHONE)
    assert (response.status_code, response.headers["location"]) == (303, SETTINGS)
    assert load_config().phone_address == PHONE
    assert stat.S_IMODE(settings.stat().st_mode) == 0o600
    assert box(laptop.get(SETTINGS)) == PHONE


@pytest.mark.parametrize("typed", [PHONE.upper(), f"{PHONE}/", f"  {PHONE}  ", f"\t{PHONE.upper()}/\n"])
def test_an_address_pasted_from_a_browser_is_cleaned(laptop, settings, typed):
    log_in(laptop)
    assert save(laptop, typed).status_code == 303
    assert box(laptop.get(SETTINGS)) == PHONE


@pytest.mark.parametrize(
    "typed", ['"><script>alert(1)</script>', "http://laptop.tail0000.ts.net", "https://evil.example", "x\ny", "a\x00b"]
)
def test_a_wrong_address_is_refused_and_the_old_one_stays(laptop, settings, typed):
    write(settings, PHONE)
    before = settings.read_bytes()
    log_in(laptop)
    response = save(laptop, typed)
    assert (response.status_code, "must look like" in response.text) == (400, True)
    assert settings.read_bytes() == before
    assert html.unescape(box(response)) == typed.strip()
    assert "<script>alert" not in response.text
    assert response.text.count(html.escape(typed)) <= 1  # in the box, never in the reason


def test_saving_it_empty_turns_it_off(laptop, settings):
    write(settings, PHONE)
    log_in(laptop)
    assert save(laptop, "   ").status_code == 303
    assert load_config().phone_address == ""
    assert box(laptop.get(SETTINGS)) == ""


@pytest.mark.parametrize(
    ("running", "saved", "shown"),
    [
        ("", PHONE, True),
        (PHONE, "", True),
        (PHONE, f"{PHONE}:8443", True),
        (PHONE, PHONE, False),
        (PHONE, f"{PHONE.upper()}/", False),
        ("", "", False),
    ],
)
def test_the_restart_note_shows_only_when_the_saved_address_is_not_the_running_one(
    serve, settings, running, saved, shown
):
    write(settings, saved)
    dashboard = serve(running)
    log_in(dashboard)
    assert (RESTART in dashboard.get(SETTINGS).text) is shown


def test_a_new_address_does_nothing_until_a_restart(serve, settings):
    dashboard = serve("")
    log_in(dashboard)
    assert save(dashboard, PHONE).status_code == 303
    assert dashboard.app.state.phone_host == ""
    response = dashboard.get("/login", headers={"Host": HOST} | IDENTITY)
    assert (response.status_code, UNKNOWN_PHONE in words(response)) == (400, True)


def test_the_phone_sees_the_address_but_cannot_change_it(phone, settings):
    write(settings, PHONE)
    before = settings.read_bytes()
    cookie, session = phone_login(phone)
    page = phone.get(SETTINGS, headers=cookie)
    assert (page.status_code, PHONE in page.text, "Change it on the laptop" in page.text) == (200, True, True)
    assert 'name="phone_address"' not in page.text and f'action="{SETTINGS}"' not in page.text
    for typed in ("", "https://other.tail0000.ts.net"):
        data = {"phone_address": typed, "csrf_token": session.csrf}
        response = phone.post(SETTINGS, data=data, headers=cookie | ORIGIN, follow_redirects=False)
        assert (response.status_code, "Change the phone address on the laptop." in words(response)) == (403, True)
    assert settings.read_bytes() == before


def test_laptop_only_refuses_only_on_the_phone_address():
    assert security.laptop_only(SimpleNamespace(state=SimpleNamespace(remote=False)), "Not here.") is None
    with pytest.raises(HTTPException) as refusal:
        security.laptop_only(SimpleNamespace(state=SimpleNamespace(remote=True)), "Not here.")
    assert (refusal.value.status_code, refusal.value.detail) == (403, "Not here.")


@pytest.mark.parametrize("broken", ["data_dir: [\n", "surprise: 1\n"])
def test_a_broken_settings_file_is_a_plain_problem_on_the_page(laptop, settings, broken):
    settings.write_text(broken, encoding="utf-8")
    log_in(laptop)
    page = laptop.get(SETTINGS)
    assert (page.status_code, 'class="error"' in page.text) == (200, True)
    assert "Something went wrong" not in page.text and RESTART not in page.text
    assert save(laptop, PHONE).status_code == 400
    assert settings.read_text(encoding="utf-8") == broken


def test_a_wrong_address_typed_into_the_file_is_shown_and_can_be_put_right(laptop, settings):
    write(settings, "http://laptop.tail0000.ts.net")
    log_in(laptop)
    page = laptop.get(SETTINGS)
    assert (page.status_code, "must look like" in page.text, box(page)) == (200, True, "")
    assert save(laptop, PHONE).status_code == 303
    assert load_config().phone_address == PHONE


def test_settings_has_one_link_to_the_phone_page_on_both_addresses(laptop, phone, settings):
    cookie, _ = phone_login(phone)
    log_in(laptop)
    for page in (laptop.get("/settings"), phone.get("/settings", headers=cookie)):
        assert page.text.count(f'href="{SETTINGS}"') == 1


def test_the_login_page_says_where_to_reset_a_forgotten_password(either):
    reset = "<code>python -m app.web reset-password</code>."
    assert f'<p class="hint">Forgot it? Stop the dashboard and run {reset}</p>' in either.get("/login").text


def test_the_phones_login_page_says_the_reset_is_on_the_laptop(phone):
    reset = "<code>python -m app.web reset-password</code>."
    for page in (phone.get("/login"), log_in_with(phone, "wrong guess", ORIGIN)):
        assert f"Forgot it? Reset it on the laptop: stop the dashboard and run {reset}" in page.text
        assert "Forgot it? Stop the dashboard" not in page.text


EXPORT = "/settings/export"
ON_THE_LAPTOP = "Export on the laptop: it writes unencrypted files there."


@pytest.fixture
def started(laptop, monkeypatch):
    """Presses of Export, kept and not run."""
    found = []
    monkeypatch.setattr(laptop.app.state.exporter, "start", lambda start, end: found.append((start, end)))
    return found


@pytest.mark.parametrize("route", ["/folder", "/run", "/schedule"])
def test_no_press_on_the_export_page_works_from_the_phone(phone, laptop, settings, started, tmp_path, route):
    write_export(settings, tmp_path)
    before = settings.read_bytes()
    cookie, session = phone_login(phone)
    form = {"csrf_token": session.csrf, "tick": "on", "folder": str(tmp_path / "new"), "every_days": "7"}
    response = phone.post(EXPORT + route, data=form, headers=cookie | ORIGIN, follow_redirects=False)
    assert (response.status_code, ON_THE_LAPTOP in words(response)) == (403, True)
    assert settings.read_bytes() == before and started == [] and not (tmp_path / "new").exists()


def write_export(settings, folder):
    settings.write_text(f'{settings.read_text(encoding="utf-8")}export:\n  folder: "{folder}"\n', encoding="utf-8")


def test_a_forged_press_from_the_phone_is_refused_all_the_same(phone, laptop, settings, started):
    cookie, _ = phone_login(phone)
    response = phone.post(f"{EXPORT}/run", data={"tick": "on"}, headers=cookie | ORIGIN, follow_redirects=False)
    assert response.status_code == 403 and started == []


def test_on_the_phone_the_page_only_says_how_it_is_going(phone, laptop, settings):
    laptop.app.state.exporter.notice = "1 new folder, 5 files added"
    cookie, _ = phone_login(phone)
    response = phone.get(EXPORT, headers=cookie)
    assert response.status_code == 200 and ON_THE_LAPTOP in words(response)
    assert "These files are not encrypted." in response.text and "1 new folder, 5 files added" in response.text
    assert "<form" not in response.text.split("<h1>Export</h1>")[1].split("</main>")[0]
    assert 'name="folder"' not in response.text and 'name="tick"' not in response.text
    laptop.app.state.exporter.notice = None


def test_the_phone_is_told_nothing_of_the_folder(phone, laptop, settings, tmp_path):
    write_export(settings, tmp_path)
    cookie, _ = phone_login(phone)
    assert str(tmp_path) not in phone.get(EXPORT, headers=cookie).text


def test_the_status_part_still_answers_on_the_phone(phone, laptop, settings):
    exporter = laptop.app.state.exporter
    exporter._thread, exporter.done, exporter.total = object(), 3, 9
    cookie, _ = phone_login(phone)
    response = phone.get(f"{EXPORT}/status", headers=cookie)
    exporter._thread = None
    assert response.status_code == 200 and "Exporting 3 of 9 applications…" in response.text


def test_on_the_laptop_the_page_keeps_its_forms_and_its_presses_work(laptop, settings, started, tmp_path):
    write_export(settings, tmp_path)
    log_in(laptop)
    text = laptop.get(EXPORT).text
    assert ON_THE_LAPTOP not in words(laptop.get(EXPORT)) and text.count('method="post" action="/settings/export') == 3
    data = {"csrf_token": csrf(laptop), "tick": "on"}
    assert laptop.post(f"{EXPORT}/run", data=data, follow_redirects=False).status_code == 303 and started == [
        (None, None)
    ]
    new = {"csrf_token": csrf(laptop), "folder": str(tmp_path / "new")}
    assert laptop.post(f"{EXPORT}/folder", data=new, follow_redirects=False).status_code == 303
    assert (tmp_path / "new").is_dir()


def test_the_schedule_runs_from_the_laptops_check_whatever_a_phone_is_doing(phone, laptop, settings, conn, tmp_path):
    settings.write_text(f'{settings.read_text(encoding="utf-8")}export:\n  folder: "{tmp_path}"\n  every_days: 1\n',
                        encoding="utf-8")  # fmt: skip
    cookie, session = phone_login(phone)
    phone.post(f"{EXPORT}/run", data={"csrf_token": session.csrf, "tick": "on"}, headers=cookie | ORIGIN)
    exporter, begun = laptop.app.state.exporter, []
    exporter._begin = begun.append
    exporter.tick(conn)
    assert begun == [None]


def fake_google(client):
    set_secret(CLIENT_ID, "123-made-up.apps.googleusercontent.com")
    set_secret(CLIENT_SECRET, "made-up-secret")
    answers = {"/token": [{"refresh_token": LASTING, "access_token": "a", "scope": SCOPE}]}
    client.app.state.gmail_transport = httpx.MockTransport(FakeGoogle(answers))


def press_connect(client):
    """Press Connect on the laptop; the one-time state Google would be sent."""
    reply = client.get(CONNECT, follow_redirects=False)
    assert urlsplit(reply.headers["location"]).netloc == "accounts.google.com"
    return parse_qs(urlsplit(reply.headers["location"]).query)["state"][0]


def test_connect_is_refused_on_the_phone_before_anything_is_kept(phone):
    fake_google(phone)
    cookie, _ = phone_login(phone)
    response = phone.get(CONNECT, headers=cookie, follow_redirects=False)
    assert (response.status_code, GMAIL_ON_THE_LAPTOP in words(response)) == (403, True)
    assert "location" not in response.headers and "127.0.0.1" not in response.text
    assert phone.app.state.gmail_pending is None


def test_googles_answer_on_the_phone_is_refused_and_leaves_the_laptops_connect_waiting(laptop, phone):
    fake_google(laptop)
    log_in(laptop)
    state = press_connect(laptop)
    laptop.cookies.clear()
    answer = {"state": state, "code": "made-up-one-time-code"}
    refused = phone.get(gmail_settings.CALLBACK, params=answer, follow_redirects=False)
    assert (refused.status_code, GMAIL_ON_THE_LAPTOP in words(refused)) == (403, True)
    assert laptop.app.state.gmail_pending["state"] == state and get_secret(REFRESH) is None
    done = laptop.get(gmail_settings.CALLBACK, params=answer, follow_redirects=False)
    assert (done.status_code, "Gmail connected." in done.text, get_secret(REFRESH)) == (200, True, LASTING)


def test_gmail_settings_on_the_phone_say_where_to_connect_and_keep_the_rest(phone):
    set_secret(REFRESH, LASTING)  # connected
    cookie, session = phone_login(phone)
    page = phone.get("/settings/gmail", headers=cookie)
    assert (page.status_code, "Connect Gmail on the laptop" in page.text) == (200, True)
    assert f'href="{CONNECT}"' not in page.text
    for form in (
        "/settings/gmail/sync",
        "/settings/gmail/disconnect",
        "/settings/gmail/period",
        "/settings/gmail/client",
    ):
        assert f'action="{form}"' in page.text
    data = {"csrf_token": session.csrf}
    assert (
        phone.post("/settings/gmail/sync", data=data, headers=cookie | ORIGIN, follow_redirects=False).status_code
        == 303
    )


def test_connect_on_the_laptop_is_as_today_with_the_setting_on_or_empty(either):
    fake_google(either)
    log_in(either)
    page = either.get("/settings/gmail")
    assert f'href="{CONNECT}"' in page.text and "Connect Gmail on the laptop" not in page.text
    state = press_connect(either)
    assert either.app.state.gmail_pending["redirect"] == f"{ADDRESS}{gmail_settings.CALLBACK}"
    either.cookies.clear()  # Google's redirect comes without the login cookie
    answer = {"state": state, "code": "made-up-one-time-code"}
    done = either.get(gmail_settings.CALLBACK, params=answer, follow_redirects=False)
    assert (done.status_code, get_secret(REFRESH)) == (200, LASTING)


def test_log_out_all_on_the_phone_ends_the_laptop_too_and_clears_the_phones_cookie(laptop, phone):
    log_in(laptop)
    cookie, session = phone_login(phone)
    data = {"csrf_token": session.csrf}
    response = phone.post(LOG_OUT_ALL, data=data, headers=cookie | ORIGIN, follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/login")
    assert re.fullmatch(
        r'__Host-auto_apply_session=""; expires=[^;]+; Max-Age=0; Path=/; SameSite=lax; Secure',
        response.headers["Set-Cookie"],
    )
    assert not phone.app.state.logins.sessions
    assert laptop.get("/", follow_redirects=False).headers["location"] == "/login"


def test_log_out_all_on_the_laptop_ends_the_phone_too_and_clears_todays_cookie(laptop, phone):
    cookie, _ = phone_login(phone)
    log_in(laptop)
    response = laptop.post(LOG_OUT_ALL, data={"csrf_token": csrf(laptop)}, follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/login")
    assert re.fullmatch(
        rf'{security.COOKIE}=""; expires=[^;]+; Max-Age=0; Path=/; SameSite=lax', response.headers["Set-Cookie"]
    )
    assert phone.get("/", headers=cookie, follow_redirects=False).headers["location"] == "/login"


def test_log_out_all_needs_a_login_the_token_and_a_post(laptop, phone):
    cookie, session = phone_login(phone)
    assert laptop.post(LOG_OUT_ALL, follow_redirects=False).headers["location"] == "/login"
    no_token = phone.post(LOG_OUT_ALL, headers=cookie | ORIGIN, follow_redirects=False)
    assert (no_token.status_code, EXPIRED in words(no_token)) == (403, True)
    another_site = {"csrf_token": session.csrf}
    sent = cookie | {"Origin": "https://attacker.example"}
    assert phone.post(LOG_OUT_ALL, data=another_site, headers=sent, follow_redirects=False).status_code == 403
    assert phone.get(LOG_OUT_ALL, headers=cookie).status_code == 405
    assert len(phone.app.state.logins.sessions) == 1


def test_pressing_log_out_all_twice_is_harmless(phone):
    cookie, session = phone_login(phone)
    data = {"csrf_token": session.csrf}
    for _ in range(2):
        response = phone.post(LOG_OUT_ALL, data=data, headers=cookie | ORIGIN, follow_redirects=False)
        assert (response.status_code, response.headers["location"]) == (303, "/login")


@pytest.mark.parametrize(
    ("running", "saved", "shown"), [(PHONE, PHONE, True), (PHONE, "", True), ("", PHONE, False), ("", "", False)]
)
def test_the_log_out_all_button_shows_while_a_phone_can_be_logged_in(serve, settings, running, saved, shown):
    write(settings, saved)
    dashboard = serve(running)
    log_in(dashboard)
    assert (f'action="{LOG_OUT_ALL}"' in dashboard.get(SETTINGS).text) is shown
    assert "log-out-all" not in dashboard.get("/settings").text


def test_the_phone_has_the_log_out_all_button_with_its_hint(phone, settings):
    write(settings, PHONE)
    cookie, _ = phone_login(phone)
    text = words(phone.get(SETTINGS, headers=cookie))
    assert f'action="{LOG_OUT_ALL}"' in text
    assert (
        "If you lose your phone, first remove it in Tailscale, then press this. You'll log in again here too." in text
    )


def applied_cards(conn, count):
    with conn:
        for _ in range(count):
            job = add_job(conn, "Robotics Intern")
            conn.execute(
                "INSERT INTO tracker (job_id, stage, applied_on, changed_on) VALUES (?, 'applied', '2026-11-01', ?)",
                (job, collect.now()),
            )


def test_the_phone_pages_a_full_lane_with_a_link_to_the_next_100(phone, conn):
    applied_cards(conn, 230)
    cookie, _ = phone_login(phone)
    first, second, third = (
        phone.get(f"/tracker?all=applied&page={number}", headers=cookie).text for number in (1, 2, 3)
    )
    assert [len(APPLIED_CARD.findall(text)) for text in (first, second, third)] == [100, 100, 30]
    assert '<a href="/tracker?all=applied&amp;page=2#lane-applied">Show the next 100</a>' in first
    assert '<a href="/tracker?all=applied&amp;page=3#lane-applied">Show the next 100</a>' in second
    assert "Show the next" not in third
    assert not set(APPLIED_CARD.findall(first)) & set(APPLIED_CARD.findall(second))


@pytest.mark.parametrize(
    ("number", "cards"),
    [("abc", 100), ("-1", 100), ("0", 100), ("²", 100), ("9" * 10, 100), ("999999999", 30), ("", 100)],
)
def test_a_wrong_page_number_on_the_phone_never_breaks_the_lane(phone, conn, number, cards):
    applied_cards(conn, 130)
    cookie, _ = phone_login(phone)
    response = phone.get("/tracker", params={"all": "applied", "page": number}, headers=cookie)
    assert (response.status_code, len(APPLIED_CARD.findall(response.text))) == (200, cards)


def test_the_phone_s_normal_board_is_as_before(phone, conn):
    applied_cards(conn, 130)
    cookie, _ = phone_login(phone)
    text = phone.get("/tracker?page=2", headers=cookie).text
    assert len(APPLIED_CARD.findall(text)) == 30 and "Show all 130" in text and "Show the next" not in text


def test_the_laptop_shows_a_full_lane_whole_whatever_the_page_with_the_setting_on_or_empty(either, conn):
    applied_cards(conn, 130)
    log_in(either)
    text = either.get("/tracker?all=applied&page=2").text
    assert len(APPLIED_CARD.findall(text)) == 130 and "Show the next" not in text


PICKER = "/tracker/emails/abc123/picker"
JOB_SELECT = '<select name="job" required>'
LOADING = '<p class="hint">Loading the job list… If nothing comes, reload this page.</p>'
HTMX = {"HX-Request": "true"}


def many_cards(conn, count):
    with conn:
        for number in range(count):
            job = add_job(conn, "Robotics Intern", company=f"Company {number}")
            version(conn, job)


def two_cards(conn):
    with conn:
        found = [
            add_job(conn, title, company=company)
            for company, title in (("Acme", "ML Intern"), ("Globex", "Data Scientist"))
        ]
        for job in found:
            version(conn, job)
    return found


def nothing_inline(text):
    assert not re.search(r"<script(?![^>]* src=)|<style| style=| on[a-z]+=", text)


def test_the_phone_gets_the_list_without_the_job_lists(phone, conn):
    many_cards(conn, 400)
    for number in range(20):
        email(conn, f"message{number}")
    cookie, _ = phone_login(phone)
    text = phone.get("/tracker/emails", headers=cookie).text
    assert JOB_SELECT not in text and "Robotics Intern</option>" not in text
    assert text.count('hx-trigger="toggle once"') == 20
    opens = 'hx-get="/tracker/emails/message7/picker" hx-trigger="toggle once" hx-target="find p" hx-swap="outerHTML"'
    assert f"<details {opens}>" in text
    assert text.count(LOADING) == 20  # a failed fetch leaves these words
    nothing_inline(text)


def test_the_phone_s_picker_brings_the_job_list_of_that_e_mail(phone, conn):
    many_cards(conn, 3)
    email(conn, "abc123")
    cookie, _ = phone_login(phone)
    response = phone.get(PICKER, headers=cookie | HTMX)
    assert (response.status_code, response.headers["Cache-Control"]) == (200, "no-store")
    text = response.text
    assert text.lstrip().startswith("<form") and "<html" not in text
    assert 'action="/tracker/emails/abc123/link"' in text and "Which job" in text
    assert JOB_SELECT in text and text.count("Robotics Intern</option>") == 3
    nothing_inline(text)


def test_the_picker_says_another_job_when_likely_cards_are_shown(phone, conn):
    jobs = two_cards(conn)
    email(conn, "abc123", candidates=f"[{jobs[0]}]")
    cookie, _ = phone_login(phone)
    page = phone.get("/tracker/emails", headers=cookie).text
    assert "<summary>Another job</summary>" in page and "This is Acme · ML Intern" in page
    assert "Another job" in phone.get(PICKER, headers=cookie).text


def test_the_picker_answers_only_for_an_e_mail_waiting_on_the_list(phone, conn):
    email(conn, "abc123", to_check=0)
    cookie, _ = phone_login(phone)
    for address in (PICKER, "/tracker/emails/nothing/picker", "/tracker/emails/not%20an%20id/picker"):
        assert phone.get(address, headers=cookie).status_code == 404


def test_the_picker_needs_a_login_like_every_page(phone, conn):
    email(conn, "abc123")
    assert phone.get(PICKER, follow_redirects=False).headers["location"] == "/login"
    answer = phone.get(PICKER, headers=HTMX, follow_redirects=False)
    assert (answer.status_code, answer.headers["HX-Redirect"]) == (204, "/login")


def test_the_form_from_the_picker_links_the_e_mail_on_the_phone(phone, conn):
    jobs = two_cards(conn)
    email(conn, "abc123")
    cookie, _ = phone_login(phone)
    token = re.search(r'name="csrf_token" value="([^"]+)"', phone.get(PICKER, headers=cookie).text)[1]
    form = {"job": str(jobs[0]), "label": "interview", "csrf_token": token}
    reply = phone.post("/tracker/emails/abc123/link", data=form, headers=cookie | ORIGIN, follow_redirects=False)
    assert reply.headers["location"] == "/tracker/emails?done=moved" and tracker.stage(conn, jobs[0]) == "interview"


def test_the_laptop_keeps_the_job_list_in_the_page_with_the_setting_on_or_empty(either, conn):
    two_cards(conn)
    email(conn, "abc123")
    log_in(either)
    text = either.get("/tracker/emails").text
    assert "Globex · Data Scientist</option>" in text
    assert "/picker" not in text and "toggle once" not in text and LOADING not in text
    assert (
        '  <form method="post" action="/tracker/emails/abc123/link" class="stacked">\n'
        f'    <input type="hidden" name="csrf_token" value="{csrf(either)}">\n'
        '    <label>Which job <select name="job" required>\n'
        '      <option value="" selected disabled>Choose…</option>\n'
    ) in text
    nothing_inline(text)


SHARED = "https://jobs.example.com/1"


def job_box(response):
    return html.unescape(re.search(r'<input name="link"[^>]* value="([^"]*)"', response.text)[1])


def jobs_saved(conn):
    return conn.execute("SELECT count(*) FROM jobs").fetchone()[0]


@pytest.mark.parametrize(
    ("params", "filled"),
    [
        ({"link": SHARED}, SHARED),
        ({"text": f"Look at this {SHARED} role"}, SHARED),
        ({"link": SHARED, "text": "Other https://other.example/2"}, SHARED),
        ({"text": f"HTTPS://JOBS.EXAMPLE.COM/1 and {SHARED}"}, "HTTPS://JOBS.EXAMPLE.COM/1"),
        ({"link": "javascript:alert(1)"}, ""),
        ({"link": "ftp://jobs.example.com/1"}, ""),
        ({"text": "A job with no link in it"}, ""),
        ({"link": "", "text": ""}, ""),
        ({}, ""),
    ],
)
def test_a_shared_link_fills_the_box_on_the_phone_and_saves_nothing(phone, conn, params, filled):
    cookie, _ = phone_login(phone)
    before = jobs_saved(conn)
    response = phone.get("/add", params=params, headers=cookie)
    assert (response.status_code, job_box(response)) == (200, filled)
    assert jobs_saved(conn) == before


def test_a_shared_link_is_cut_at_the_box_s_limit_and_escaped(phone):
    cookie, _ = phone_login(phone)
    long = phone.get("/add", params={"link": f"{SHARED}/{'a' * 3000}"}, headers=cookie)
    assert len(job_box(long)) == 2000
    hostile = f'{SHARED}/"><script>alert(1)</script>'
    response = phone.get("/add", params={"link": hostile}, headers=cookie)
    assert job_box(response) == hostile and "<script>alert" not in response.text


def test_add_job_after_a_share_goes_through_the_usual_rules(phone, conn):
    cookie, session = phone_login(phone)
    link = job_box(phone.get("/add", params={"link": "https://simplify.jobs/p/1"}, headers=cookie))
    before = jobs_saved(conn)
    data = {"link": link, "csrf_token": session.csrf}
    response = phone.post("/add/job", data=data, headers=cookie | ORIGIN)
    assert job_box(response) == "https://simplify.jobs/p/1" and jobs_saved(conn) == before
    assert re.search(r'class="result" role="status">[^<]*simplify', response.text)


def test_the_laptop_ignores_a_shared_link_with_the_setting_on_or_empty(either):
    log_in(either)
    assert job_box(either.get("/add", params={"link": SHARED, "text": SHARED})) == ""


def test_a_share_on_the_phone_comes_back_to_add_through_login(phone):
    gone = phone.get("/add", params={"link": SHARED}, follow_redirects=False)
    assert gone.status_code == 303
    address, _, sent = gone.headers["location"].partition("?next=")
    assert (address, unquote(sent)) == ("/login", f"/add?link={quote(SHARED, safe='')}")
    page = phone.get(gone.headers["location"])
    wanted = f'<input type="hidden" name="next" value="{html.escape(unquote(sent))}">'
    assert wanted in page.text or wanted.replace("&#x27;", "&#39;") in page.text
    response = phone.post(
        "/login", data={"password": PASSWORD, "next": unquote(sent)}, headers=ORIGIN, follow_redirects=False
    )
    assert (response.status_code, response.headers["location"]) == (303, unquote(sent))
    cookie = {"Cookie": response.headers["Set-Cookie"].split(";")[0]}
    assert job_box(phone.get(response.headers["location"], headers=cookie)) == SHARED


def test_the_way_back_stays_through_a_wrong_password_and_a_lock_out(phone):
    field = 'name="next" value="/add?link=x"'
    data = {"password": "wrong guess", "next": "/add?link=x"}
    for status in [401] * (security.MAX_WRONG - 1) + [429, 429]:
        response = phone.post("/login", data=data, headers=ORIGIN)
        assert (response.status_code, field in response.text) == (status, True)


@pytest.mark.parametrize(
    "address",
    [
        "https://evil.example",
        "//evil.example",
        "/settings",
        "/add",
        "/addx?a=1",
        "/\\evil.example",
        "/add?" + "a" * 3996,
        "",
    ],
)
def test_after_login_the_phone_goes_only_back_to_add(phone, address):
    assert 'name="next"' not in phone.get("/login", params={"next": address}).text
    sent = {"password": PASSWORD, "next": address}
    assert phone.post("/login", data=sent, headers=ORIGIN, follow_redirects=False).headers["location"] == "/"


def test_the_longest_way_back_is_kept(phone):
    address = "/add?" + "a" * 3995
    sent = {"password": PASSWORD, "next": address}
    assert phone.post("/login", data=sent, headers=ORIGIN, follow_redirects=False).headers["location"] == address


@pytest.mark.parametrize("address", ["/add?a=%0d%0aSet-Cookie:x=1", "/add?a=\r\nSet-Cookie:x=1"])
def test_the_way_back_can_add_no_header(phone, address):
    sent = {"password": PASSWORD, "next": address}
    response = phone.post("/login", data=sent, headers=ORIGIN, follow_redirects=False)
    assert not re.search(r"[\r\n]", response.headers["location"])
    assert [name for name, _ in response.headers.multi_items()].count("set-cookie") == 1
    assert "x=1" not in response.headers["Set-Cookie"]


def test_a_way_back_is_escaped_in_the_login_page(phone):
    address = '/add?a="><script>alert(1)</script>'
    assert "<script>alert" not in phone.get("/login", params={"next": address}).text


def test_the_laptop_has_no_way_back_with_the_setting_on_or_empty(either):
    assert either.get("/add", params={"link": "x"}, follow_redirects=False).headers["location"] == "/login"
    assert 'name="next"' not in either.get("/login", params={"next": "/add?link=x"}).text
    sent = {"password": PASSWORD, "next": "/add?link=x"}
    assert either.post("/login", data=sent, follow_redirects=False).headers["location"] == "/"


def test_only_a_plain_visit_to_add_with_a_query_is_kept(phone):
    answer = phone.get("/add", params={"link": "x"}, headers={"HX-Request": "true"}, follow_redirects=False)
    assert (answer.status_code, answer.headers["HX-Redirect"]) == (204, "/login")
    assert phone.get("/add", follow_redirects=False).headers["location"] == "/login"
    sent = phone.post("/add/job?link=x", headers=ORIGIN, follow_redirects=False)
    assert sent.headers["location"] == "/login"
    assert phone.get("/addx?link=x", follow_redirects=False).headers["location"] == "/login"
    assert phone.post("/add?link=x", headers=ORIGIN, follow_redirects=False).headers["location"] == "/login"


def test_the_phone_guide_exists_warns_first_and_gives_no_way_to_turn_funnel_on():
    page = (main.HERE / "templates" / "phone_settings.html").read_text(encoding="utf-8")
    guide = (PROJECT_DIR / re.search(r"<code>(docs/[\w.-]+)</code>", page)[1]).read_text(encoding="utf-8")
    assert "tailscale funnel --bg" not in guide
    assert guide.index("nobody can erase") < guide.index("tailscale serve")


def test_the_guides_name_only_the_made_up_address():
    for name in ("phone-setup.md", "cloud-checklist.md"):
        text = (PROJECT_DIR / "docs" / name).read_text(encoding="utf-8")
        assert set(re.findall(r"[\w.-]+\.ts\.net", text)) <= {HOST}
