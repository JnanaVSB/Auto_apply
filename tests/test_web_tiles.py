"""The Tiles page: what it shows, Confirm, a refused confirm, and a resume that changed while the page was open."""

import re

import pytest
from helpers import csrf, log_in
from test_tile_sets import form_of
from test_tiles import RESUME_CLS

from app.documents import tile_sets
from app.documents.tiles import read_tiles


@pytest.fixture
def client(client, data_dir):
    (data_dir / "resume.tex").write_text(RESUME_CLS, encoding="utf-8")
    log_in(client)
    return client


def resume_of(text):
    return re.search(r'name="resume" value="([0-9a-f]{64})"', text)[1]


def confirm(client, **changes):
    page = client.get("/tiles").text
    form = form_of(read_tiles(RESUME_CLS).tiles, **changes) | {"resume": resume_of(page), "csrf_token": csrf(client)}
    return client.post("/tiles", data=form, follow_redirects=False)


def test_the_page_proposes_tiles_from_the_resume(client):
    text = client.get("/tiles").text
    assert "Not confirmed yet" in text and 'value="Toy Driver"' in text
    assert "A stray sentence the reader can&#39;t place." in text  # unplaced text is shown, never dropped
    assert 'fill in its <a href="/questionnaire">Contact section</a>' in text
    assert "Alex Example" not in text  # the header is never a tile


def test_confirm_saves_once_and_the_page_then_shows_it(client, data_dir, conn):
    answer = confirm(client)
    assert answer.status_code == 303 and answer.headers["location"] == "/tiles?confirmed=1"
    assert "Confirmed on" in client.get("/tiles?confirmed=1").text
    assert confirm(client).headers["location"] == "/tiles?unchanged=1"  # pressing it again saves nothing
    assert conn.execute("SELECT count(*) FROM tile_sets").fetchone()[0] == 1


def test_a_changed_resume_shows_what_changed(client, data_dir):
    confirm(client)
    (data_dir / "resume.tex").write_text(RESUME_CLS.replace("Toy Driver", "Kite"), encoding="utf-8")
    text = client.get("/tiles").text
    assert "Your resume changed since you confirmed" in text
    assert '<span class="mark">new tile</span> Kite' in text and "no longer in your resume</span> Toy Driver" in text


def test_a_refused_confirm_keeps_what_was_typed(client, conn):
    answer = confirm(client, tile_1_name="", tile_2_rest="<b>typed</b>")
    assert answer.status_code == 200 and "Not confirmed: Tile 2, name: is empty" in answer.text
    assert 'value="&lt;b&gt;typed&lt;/b&gt;"' in answer.text
    assert conn.execute("SELECT count(*) FROM tile_sets").fetchone()[0] == 0


def test_a_resume_changed_while_the_page_was_open_is_never_confirmed(client, data_dir, conn):
    page = client.get("/tiles").text
    (data_dir / "resume.tex").write_text(RESUME_CLS.replace("Toy Driver", "Kite"), encoding="utf-8")
    form = form_of(read_tiles(RESUME_CLS).tiles) | {"resume": resume_of(page), "csrf_token": csrf(client)}
    answer = client.post("/tiles", data=form)
    assert "Your resume changed while this page was open" in answer.text
    assert tile_sets.latest(conn) is None


def test_no_resume_says_how_to_add_one(client, data_dir):
    (data_dir / "resume.tex").unlink()
    assert "Resume not found: save your LaTeX resume as" in client.get("/tiles").text


def test_the_contact_check_uses_the_questionnaire(client):
    client.post("/questionnaire", data={"contact.email": "sam@example.com", "csrf_token": csrf(client)})
    assert "doesn't show the email in your" in client.get("/tiles").text


def test_your_corrections_are_shown_while_the_resume_is_unchanged(client):
    confirm(client, tile_7_name="Toy Driver, renamed")
    text = client.get("/tiles").text
    assert 'value="Toy Driver, renamed"' in text and 'value="Toy Driver"' not in text
