"""The Look section on Settings: colours and mode kept in this browser's cookie, put on every page's <html>, refused
without the safety token or with a value not in the lists."""

import pytest
from helpers import add_job, csrf, log_in

from app import config
from app.web import look

PLAIN = '<html lang="en">'


@pytest.fixture
def client(client, conn):
    with conn:
        add_job(conn, "ML Intern")
    log_in(client)
    return client


def choose(client, scheme, mode, token=True):
    form = {"scheme": scheme, "mode": mode} | ({"csrf_token": csrf(client)} if token else {})
    return client.post("/look", data=form, follow_redirects=False)


def test_the_look_is_kept_and_every_page_carries_it(client):
    reply = choose(client, "E", "dark")
    assert reply.status_code == 303 and reply.headers["location"] == "/settings?look=changed#look"
    cookie = reply.headers["set-cookie"]
    assert cookie.startswith(f"{look.LOOK_COOKIE}=E.dark;") and "Max-Age=31536000" in cookie and "Path=/" in cookie
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Secure" not in cookie
    for page in ("/", "/ready", "/jobs/1", "/settings", "/nowhere", "/login"):
        assert '<html lang="en" data-palette="E" data-theme="dark">' in client.get(page).text, page
    assert not config.CONFIG_PATH.exists()  # the settings file is never written


@pytest.mark.parametrize(
    ("scheme", "mode", "html"),
    [("today", "auto", PLAIN), ("A", "auto", '<html lang="en" data-palette="A">'),
     ("today", "light", '<html lang="en" data-theme="light">')],
)  # fmt: skip
def test_the_defaults_add_nothing(client, scheme, mode, html):
    assert PLAIN in client.get("/").text  # nothing chosen yet
    choose(client, scheme, mode)
    assert html in client.get("/").text


@pytest.mark.parametrize(
    ("scheme", "mode"),
    [("H", "auto"), ("a", "dark"), ("today", "dim"), ("A", "Dark"), ("", ""), ("A.dark", "dark"), ("A", "dark.x")],
)
def test_a_value_not_in_the_lists_is_refused(client, scheme, mode):
    reply = choose(client, scheme, mode)
    assert reply.status_code == 400 and "set-cookie" not in reply.headers


def test_the_look_needs_the_safety_token(client):
    reply = choose(client, "A", "dark", token=False)
    assert reply.status_code == 403 and "set-cookie" not in reply.headers


@pytest.mark.parametrize("value", ["", "E", "E.", ".dark", "e.dark", "E.Dark", "E dark", "E.dark.x", '"><b>.dark'])
def test_a_damaged_cookie_gives_the_defaults(client, value):
    client.cookies.set(look.LOOK_COOKIE, value)
    assert PLAIN in client.get("/").text


def test_settings_shows_the_look_in_use_and_confirms_only_a_change(client):
    text = client.get("/settings").text
    assert '<option value="today" selected>' in text and '<option value="auto" selected>' in text
    assert "Look saved for this browser." not in text
    text = client.get(choose(client, "C", "light").headers["location"]).text
    assert '<option value="C" selected>' in text and '<option value="light" selected>' in text
    assert "Look saved for this browser." in text and "Saved." not in text
