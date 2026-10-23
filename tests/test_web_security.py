"""Dashboard login and safety checks, with a throwaway database, a fake clock and the fake keychain."""

import hashlib
import threading
from contextlib import closing

import pytest
from helpers import PASSWORD, csrf, log_in

from app import collect, db
from app.config import Config
from app.web import __main__ as web_command
from app.web import main, security


def test_password_hash_is_salted_and_never_holds_the_password():
    first, second = security.hash_password(PASSWORD), security.hash_password(PASSWORD)
    assert first != second and PASSWORD not in first
    assert security.password_matches(PASSWORD, first)
    assert not security.password_matches(PASSWORD + "!", first)


def test_pages_need_a_login(client):
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    assert client.get("/", headers={"HX-Request": "true"}).headers["HX-Redirect"] == "/login"
    assert client.get("/static/app.css").status_code == 200


def test_right_password_logs_in_with_a_safe_cookie(client):
    response = log_in(client)
    assert (response.status_code, response.headers["location"]) == (303, "/")
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert "<h1>Jobs</h1>" in client.get("/").text


def test_a_new_login_gets_a_new_session(client):
    log_in(client)
    first = client.cookies[security.COOKIE]
    log_in(client)
    assert client.cookies[security.COOKIE] != first


def test_wrong_passwords_lock_login_for_15_minutes(client, clock):
    for _ in range(security.MAX_WRONG - 1):
        assert "Wrong password." in log_in(client, "wrong guess").text
    response = log_in(client, "wrong guess")
    assert (response.status_code, "Try again in 15 minute(s)" in response.text) == (429, True)
    assert log_in(client).status_code == 429  # even the right password, while locked
    clock.now += security.LOCKOUT_SECONDS
    assert log_in(client).status_code == 303


def test_a_session_ends_after_30_idle_minutes(client, clock):
    log_in(client)
    clock.now += security.IDLE_SECONDS - 60
    assert client.get("/", follow_redirects=False).status_code == 200
    clock.now += security.IDLE_SECONDS + 1
    assert client.get("/", follow_redirects=False).status_code == 303


def test_a_session_ends_after_12_hours_even_when_in_use(client, clock):
    log_in(client)
    for _ in range(security.MAX_SESSION_SECONDS // 1200):  # a page every 20 minutes for 12 hours
        clock.now += 1200
        assert client.get("/", follow_redirects=False).status_code == 200
    clock.now += 1200
    assert client.get("/", follow_redirects=False).status_code == 303


def test_logout_ends_the_session(client):
    log_in(client)
    token = client.cookies[security.COOKIE]
    assert client.post("/logout", data={"csrf_token": csrf(client)}, follow_redirects=False).status_code == 303
    client.cookies.set(security.COOKIE, token)  # an old cookie no longer works
    assert client.get("/", follow_redirects=False).status_code == 303


@pytest.mark.parametrize(
    ("form", "headers"),
    [
        ({}, {}),  # no anti-forgery token
        ({"csrf_token": "guessed"}, {}),
        ({"csrf_token": "OK"}, {"Origin": "https://evil.example"}),  # sent from another website
    ],
)
def test_changes_without_the_right_token_or_from_another_site_are_refused(client, form, headers):
    log_in(client)
    form = {name: csrf(client) if value == "OK" else value for name, value in form.items()}
    response = client.post("/logout", data=form, headers=headers)
    assert (response.status_code, "This page has expired" in response.text) == (403, True)
    assert client.get("/", follow_redirects=False).status_code == 200  # still logged in: nothing happened


def test_htmx_sends_the_token_in_a_header(client):
    log_in(client)
    assert 'hx-headers=\'{"X-CSRF-Token": "' in client.get("/").text
    response = client.post("/logout", headers={"X-CSRF-Token": csrf(client)}, follow_redirects=False)
    assert response.status_code == 303


def test_only_this_computers_address_is_served(client):
    response = client.get("/login", headers={"Host": "attacker.example:8765"})
    assert (response.status_code, "not allowed" in response.text) == (400, True)
    assert client.get("/login", headers={"Host": "localhost:8765"}).status_code == 200


def test_every_page_has_the_safety_headers(client):
    for response in (client.get("/login"), client.get("/", follow_redirects=False), client.get("/nowhere")):
        policy = response.headers["Content-Security-Policy"]
        assert "default-src 'none'" in policy and "script-src 'self'" in policy and "frame-ancestors 'none'" in policy
        assert response.headers["Referrer-Policy"] == "same-origin"
        assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_too_much_form_data_is_refused(client):
    response = client.post("/login", content=b"password=" + b"x" * main.MAX_FORM_BYTES)
    assert response.status_code == 413


def test_unexpected_errors_show_one_line_and_no_details(client, monkeypatch, caplog):
    def broken(*args):
        raise ValueError("private job text")

    monkeypatch.setattr(security, "password_matches", broken)
    response = log_in(client)
    assert response.status_code == 500 and "Something went wrong (ValueError)" in response.text
    assert "private job text" not in response.text + caplog.text and "Traceback" not in response.text


def test_missing_page_says_so(client):
    log_in(client)
    assert "Page not found." in client.get("/nowhere").text


@pytest.fixture
def command(data_dir, monkeypatch, capsys):
    """Run `python -m app.web` with typed password answers; never starts a real server or browser."""
    served = []
    monkeypatch.setattr(web_command, "load_config", lambda: Config(data_dir=data_dir))
    monkeypatch.setattr(web_command.uvicorn, "run", lambda app, **options: served.append(options))
    monkeypatch.setattr(web_command.threading, "Timer", lambda *args: type("T", (), {"start": lambda self: None})())
    monkeypatch.setattr(web_command, "_port_free", lambda port: True)  # never ask what runs on this computer

    def run(argv=(), answers=()):
        replies = iter(answers)
        monkeypatch.setattr(db.getpass, "getpass", lambda prompt: next(replies))
        return web_command.main(list(argv)), capsys.readouterr(), served

    return run


def test_start_serves_this_computer_only_without_a_request_log(command):
    code, output, served = command()
    assert (code, served[0]["host"], served[0]["access_log"]) == (0, "127.0.0.1", False)
    assert "http://127.0.0.1:8765" in output.out


def test_first_start_asks_for_a_password(command, data_dir):
    with closing(db.connect(data_dir)) as conn, conn:
        conn.execute("DELETE FROM dashboard_login")
    new = "another fake passphrase"
    code, output, served = command(answers=[new, new])
    assert (code, "Dashboard password saved." in output.out, len(served)) == (0, True, 1)
    with closing(db.connect(data_dir)) as conn:
        assert security.password_matches(new, conn.execute("SELECT password_hash FROM dashboard_login").fetchone()[0])


@pytest.mark.parametrize(
    ("answers", "message"),
    [(["short"], "dashboard password must be at least 12"), (["fake passphrase 1", "fake passphrase 2"], "match")],
)
def test_reset_password_refuses_weak_or_mistyped_passwords(command, data_dir, answers, message):
    code, output, served = command(["reset-password"], answers)
    assert (code, message in output.err, served) == (1, True, [])
    with closing(db.connect(data_dir)) as conn:  # the old password still works
        assert security.password_matches(
            PASSWORD, conn.execute("SELECT password_hash FROM dashboard_login").fetchone()[0]
        )


def test_reset_password_does_not_start_the_dashboard(command):
    code, output, served = command(["reset-password"], ["new fake passphrase", "new fake passphrase"])
    assert (code, served) == (0, [])


def test_a_second_dashboard_is_refused(command, data_dir):
    with closing(collect.lock(data_dir, web_command.DASHBOARD_LOCK)):
        code, output, served = command()
    assert (code, "already running in another window" in output.err, served) == (1, True, [])


def test_dashboard_waits_for_a_running_collection(command, data_dir):
    with closing(collect.lock(data_dir)):
        code, output, _ = command()
    assert (code, "A collection is running" in output.err) == (1, True)


def test_address_number_in_use_suggests_another(command, monkeypatch):
    monkeypatch.setattr(web_command, "_port_free", lambda port: False)
    code, output, served = command(["--port", "9000"])
    assert (code, "python -m app.web --port 9001" in output.err, served) == (1, True, [])


def test_start_without_a_database_points_to_setup(command, data_dir):
    (data_dir / db.DB_FILE).unlink()
    code, output, served = command()
    assert (code, "python -m app.db setup" in output.err, served) == (1, True, [])


def test_start_with_the_keychain_entry_missing_says_how_to_recover(command, fake_keychain):
    fake_keychain.store.clear()
    code, output, served = command()
    assert (code, "Run: python -m app.db recover" in output.err, served) == (1, True, [])


@pytest.mark.parametrize("size", ["abc", "-5", "1e3", " 12"])
def test_a_form_size_that_is_not_a_number_is_refused(client, size):
    response = client.post("/login", content=b"password=x", headers={"Content-Length": size})
    assert (response.status_code, "The form could not be read." in response.text) == (400, True)


def test_a_huge_form_sent_in_pieces_is_refused_before_it_fills_memory(client):
    pieces = (b"x" * 16384 for _ in range(1000))  # 16 MB, with no size given in advance
    response = client.post("/login", content=pieces)
    assert response.status_code == 413


def test_guesses_sent_all_at_once_still_lock_login(clock):
    logins, stored = security.Logins(clock=clock), security.hash_password(PASSWORD)
    guesses = [threading.Thread(target=logins.log_in, args=("wrong guess", stored)) for _ in range(20)]
    for guess in guesses:
        guess.start()
    for guess in guesses:
        guess.join(10)
    assert logins.locked_minutes() == 15 and logins.log_in(PASSWORD, stored) is None


def test_the_local_htmx_file_is_the_recorded_release():
    """README: htmx.org@2.0.11, dist/htmx.min.js. Any change to the file, by accident or not, fails here."""
    content = (main.HERE / "static" / "htmx.min.js").read_bytes()
    assert hashlib.sha256(content).hexdigest() == "d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717"
