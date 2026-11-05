"""The phone address: who may come in through Tailscale Serve, with fake headers. No account, no Tailscale, no phone."""

import html
import re
import stat
from contextlib import ExitStack
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from helpers import ADDRESS, PASSWORD, csrf, log_in
from starlette.exceptions import HTTPException

from app import config
from app.config import Config, load_config
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
RESTART = "Saved. Restart the dashboard to use it."
SETTINGS = "/settings/phone"
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
