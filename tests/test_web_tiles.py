"""The Resume Profile page (the tiles page): what it shows, Confirm, a refused confirm, and a resume that changed
while the page was open."""

import re

import pytest
from helpers import csrf, log_in
from test_pdf import fake_tectonic
from test_tile_sets import form_of
from test_tiles import RESUME_CLS

from app.documents import pdf, tile_sets
from app.documents.tiles import read_tiles


@pytest.fixture
def client(client, data_dir):
    (data_dir / "resume.tex").write_text(RESUME_CLS, encoding="utf-8")
    log_in(client)
    return client


def resume_of(text):
    return re.search(r'name="resume" value="([0-9a-f]{64})"', text)[1]


def confirm(client, **changes):
    page = client.get("/resume-profile").text
    form = form_of(read_tiles(RESUME_CLS).tiles, **changes) | {"resume": resume_of(page), "csrf_token": csrf(client)}
    return client.post("/resume-profile", data=form, follow_redirects=False)


def test_the_page_proposes_tiles_from_the_resume(client):
    text = client.get("/resume-profile").text
    assert "<h1>Resume Profile</h1>" in text and "Not confirmed yet" in text and 'value="Toy Driver"' in text
    assert text.count('href="#new-entry">Add an entry</a>') == 2 and 'id="new-entry"' in text
    assert ">Tile" not in text and " tile" not in text.lower().replace("tile.", "")  # users never see the word
    assert "A stray sentence the reader can&#39;t place." in text  # unplaced text is shown, never dropped
    assert 'fill in its <a href="/questionnaire">Contact section</a>' in text
    assert "Alex Example" not in text  # the header is never a tile


def test_confirm_saves_once_and_the_page_then_shows_it(client, data_dir, conn):
    answer = confirm(client)
    assert answer.status_code == 303 and answer.headers["location"] == "/resume-profile?confirmed=1"
    assert "Confirmed on" in client.get("/resume-profile?confirmed=1").text
    assert confirm(client).headers["location"] == "/resume-profile?unchanged=1"  # pressing it again saves nothing
    assert conn.execute("SELECT count(*) FROM tile_sets").fetchone()[0] == 1


def test_a_changed_resume_shows_what_changed(client, data_dir):
    confirm(client)
    (data_dir / "resume.tex").write_text(RESUME_CLS.replace("Toy Driver", "Kite"), encoding="utf-8")
    text = client.get("/resume-profile").text
    assert "Your resume changed since you confirmed" in text
    assert '<span class="mark">new entry</span> Kite' in text and "no longer in your resume</span> Toy Driver" in text


def test_a_refused_confirm_keeps_what_was_typed(client, conn):
    answer = confirm(client, tile_1_name="", tile_2_rest="<b>typed</b>")
    assert answer.status_code == 200 and "Not confirmed: Entry 2, name: is empty" in answer.text
    assert 'value="&lt;b&gt;typed&lt;/b&gt;"' in answer.text
    assert conn.execute("SELECT count(*) FROM tile_sets").fetchone()[0] == 0


def test_a_resume_changed_while_the_page_was_open_is_never_confirmed(client, data_dir, conn):
    page = client.get("/resume-profile").text
    (data_dir / "resume.tex").write_text(RESUME_CLS.replace("Toy Driver", "Kite"), encoding="utf-8")
    form = form_of(read_tiles(RESUME_CLS).tiles) | {"resume": resume_of(page), "csrf_token": csrf(client)}
    answer = client.post("/resume-profile", data=form)
    assert "Your resume changed while this page was open" in answer.text
    assert tile_sets.latest(conn) is None


def test_no_resume_says_how_to_add_one(client, data_dir):
    (data_dir / "resume.tex").unlink()
    assert "Resume not found: save your LaTeX resume as" in client.get("/resume-profile").text


def test_the_contact_check_uses_the_questionnaire(client):
    client.post("/questionnaire", data={"contact.email": "sam@example.com", "csrf_token": csrf(client)})
    assert "doesn't show the email in your" in client.get("/resume-profile").text


def test_your_corrections_are_shown_while_the_resume_is_unchanged(client):
    confirm(client, tile_7_name="Toy Driver, renamed")
    text = client.get("/resume-profile").text
    assert 'value="Toy Driver, renamed"' in text and 'value="Toy Driver"' not in text


def test_the_master_pdf_is_built_from_confirmed_tiles_and_shown(client, tmp_path, monkeypatch):
    program = fake_tectonic(tmp_path)  # 2 pages, one missing character
    monkeypatch.setattr(pdf, "tectonic", lambda: program)
    assert (
        "Confirm your resume profile first"
        in client.post("/resume-profile/pdf", data={"csrf_token": csrf(client)}).text
    )
    confirm(client)
    assert "Fill in the Questionnaire" in client.post("/resume-profile/pdf", data={"csrf_token": csrf(client)}).text
    client.post("/questionnaire", data={"contact.full_name": "Alex Example", "csrf_token": csrf(client)})
    answer = client.post("/resume-profile/pdf", data={"csrf_token": csrf(client)}, follow_redirects=False)
    assert answer.headers["location"] == "/resume-profile?built=1"
    text = client.get("/resume-profile?built=1").text
    assert "Master PDF built." in text and "(2 pages)" in text and "over one page: 2 pages" in text and "☃" in text
    shown = client.get(re.search(r'href="(/documents/master/resume-v[0-9]+\.pdf)"', text)[1])
    assert shown.headers["content-type"] == "application/pdf" and shown.content == b"%PDF-fake"
    assert shown.headers["content-disposition"].startswith("inline")
    assert client.get("/documents/master/resume-v999.pdf").status_code == 404
    assert client.get("/documents/master/resume-v..%2F..%2Fapp.db.pdf").status_code == 404
