"""The Look section on Settings: colours, mode and background picture kept in this browser's cookie, put on every
page's <html>, refused without the safety token or with a value not in the lists."""

import re

import pytest
from helpers import add_job, csrf, log_in

from app import config
from app.web import background, look, main

PINES = ' data-field="pines" data-ink="dark"'  # the default picture; every signed-in page names its picture
PLAIN = f'<html lang="en"{PINES}>'  # Teal, chosen on purpose: the stylesheet's own colours
FRESH = f'<html lang="en" data-palette="G"{PINES}>'  # nothing chosen yet


@pytest.fixture
def client(client, conn):
    with conn:
        add_job(conn, "ML Intern")
    log_in(client)
    return client


def choose(client, scheme, mode, picture="pines", token=True):
    """Posts the Look form; picture=None leaves that field out, as a form from before the picture choice would."""
    form = {"scheme": scheme, "mode": mode} | ({"picture": picture} if picture is not None else {})
    form |= {"csrf_token": csrf(client)} if token else {}
    return client.post("/look", data=form, follow_redirects=False)


def test_the_look_is_kept_and_every_page_carries_it(client):
    reply = choose(client, "E", "dark")
    assert reply.status_code == 303 and reply.headers["location"] == "/settings?look=changed#look"
    cookie = reply.headers["set-cookie"]
    assert cookie.startswith(f"{look.LOOK_COOKIE}=E.dark.pines;") and "Max-Age=31536000" in cookie
    assert "Path=/" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie and "Secure" not in cookie
    for page in ("/", "/ready", "/jobs/1", "/settings", "/nowhere", "/login"):
        assert f'<html lang="en" data-palette="E" data-theme="dark"{PINES}>' in client.get(page).text, page
    assert not config.CONFIG_PATH.exists()  # the settings file is never written


@pytest.mark.parametrize(
    ("scheme", "mode", "html"),
    [("today", "auto", PLAIN), ("A", "auto", f'<html lang="en" data-palette="A"{PINES}>'),
     ("today", "light", f'<html lang="en" data-theme="light"{PINES}>')],
)  # fmt: skip
def test_a_fresh_browser_gets_g_and_the_stylesheets_own_look_adds_nothing(client, scheme, mode, html):
    assert FRESH in client.get("/").text  # nothing chosen yet
    choose(client, scheme, mode)
    assert html in client.get("/").text


@pytest.mark.parametrize(
    ("scheme", "mode", "picture"),
    [("H", "auto", "pines"), ("a", "dark", "pines"), ("today", "dim", "pines"), ("A", "Dark", "pines"), ("", "", ""),
     ("A.dark", "dark", "pines"), ("A", "dark.x", "pines"), ("A", "dark", "Pines"), ("A", "dark", "own"),
     ("A", "dark", "auto"), ("A", "dark", "pines.x"), ("A", "dark", ""), ("A", "dark", None)],
)  # fmt: skip
def test_a_value_not_in_the_lists_is_refused(client, scheme, mode, picture):
    reply = choose(client, scheme, mode, picture)
    assert reply.status_code == 400 and "set-cookie" not in reply.headers


def test_the_look_needs_the_safety_token(client):
    reply = choose(client, "A", "dark", token=False)
    assert reply.status_code == 403 and "set-cookie" not in reply.headers


@pytest.mark.parametrize(
    "value",
    ["", "E", "E.", ".dark", "e.dark", "E.Dark", "E dark", "E.dark.x", '"><b>.dark', "E.dark.", "E.dark.Pines",
     "E.dark.own", "E.dark.pines.x", "E.dark.pines.", ".dark.pines", "E.dark.login-ink.webp"],
)  # fmt: skip
def test_a_damaged_cookie_gives_the_defaults(client, value):
    client.cookies.set(look.LOOK_COOKIE, value)
    assert FRESH in client.get("/").text


def test_a_cookie_from_before_the_picture_choice_still_counts(client):
    client.cookies.set(look.LOOK_COOKIE, "E.dark")
    assert f'<html lang="en" data-palette="E" data-theme="dark"{PINES}>' in client.get("/").text
    assert '<option value="pines" selected>' in client.get("/settings").text


def test_a_chosen_picture_is_on_every_page_with_its_words_colour(client):
    choose(client, "G", "auto", "lake")
    for page in ("/", "/ready", "/jobs/1", "/settings", "/nowhere", "/login"):
        assert '<html lang="en" data-palette="G" data-field="lake" data-ink="dark">' in client.get(page).text, page
    choose(client, "today", "light", "mountains")  # the one pair of files: the mode picks, so no fixed ink
    assert '<html lang="en" data-theme="light" data-field="mountains" data-ink="mode">' in client.get("/").text
    choose(client, "today", "light", "ink")
    assert '<html lang="en" data-theme="light" data-field="ink" data-ink="dark">' in client.get("/").text


def test_the_own_picture_wins_while_it_is_set_and_the_choice_waits(client, data_dir):
    folder = data_dir / background.FOLDER
    folder.mkdir()
    (folder / "own.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 200)  # made up: only its first bytes matter
    choose(client, "G", "auto", "storm")
    for page in ("/", "/settings"):
        text = client.get(page).text
        assert '<html lang="en" data-palette="G" data-own-field data-ink="dark">' in text and "data-field" not in text
    settings = client.get("/settings").text
    assert '<option value="storm" selected>' in settings  # the choice is kept for this browser
    assert "Your own picture is showing instead of the one chosen here" in settings and "Remove my picture" in settings
    (folder / "own.jpg").unlink()  # as Remove does
    assert '<html lang="en" data-palette="G" data-field="storm" data-ink="dark">' in client.get("/").text
    assert "Your own picture is showing instead" not in client.get("/settings").text


def test_every_listed_picture_has_its_rules_in_the_stylesheet():
    """A key with no rule would show the floor colour alone (a rule naming a missing file fails the static-files
    test); the first key is the default, the only pair of files is the mountains, and every other picture is dark."""
    css = (main.HERE / "static" / "app.css").read_text()
    assert next(iter(look.PICTURES)) == "pines" and look.DEFAULT_LOOK == ("G", "auto", "pines")
    for key, picture in look.PICTURES.items():
        rules = re.findall(rf':root\[data-field="{key}"\] \{{ --field: url\("([^"]+)"\)', css)
        if key == "mountains":
            assert picture.ink == "mode" and rules == []  # the stylesheet's own pair, picked by the mode rules
        else:
            assert picture.ink == "dark"
            assert [path.endswith("-phone.webp") for path in rules] == [False, True], key  # the file, its phone copy


def test_settings_shows_the_look_in_use_and_confirms_only_a_change(client):
    text = client.get("/settings").text
    assert '<option value="G" selected>' in text and '<option value="auto" selected>' in text
    assert 'value="G" selected>G · Forest and ochre (default)<' in text and ">Teal<" in text
    assert 'value="pines" selected>Pines in fog (default)<' in text and "Background picture" in text
    assert "Look saved for this browser." not in text and "Your own picture is showing instead" not in text
    text = client.get(choose(client, "C", "light", "mist").headers["location"]).text
    assert '<option value="C" selected>' in text and '<option value="light" selected>' in text
    assert '<option value="mist" selected>' in text
    assert "Look saved for this browser." in text and "Saved." not in text
