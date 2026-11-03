"""The page frame: the menu on every logged-in page with its current page marked, and the lone login and problem
pages."""

import re

import pytest
from helpers import add_job, log_in

from app import config
from app.web import main

MENUS = re.compile(r'<nav class="(?:rail|sheet)" aria-label="Pages">(.*?)</nav>', re.S)
ICONS = set(re.findall(r'<symbol id="(i-[a-z-]+)"', (main.HERE / "static" / "icons.svg").read_text()))


def marked(text):
    """The links marked as the current page in each menu: the rail, then the phone panel."""
    return [re.findall(r'<a href="([^"]+)" aria-current="page"', menu) for menu in MENUS.findall(text)]


@pytest.mark.parametrize(
    ("address", "current"),
    [
        ("/", "/"),
        (main.LOW_SCORE, main.LOW_SCORE.replace("&", "&amp;")),
        ("/jobs/{job}", "/"),
        ("/add", "/add"),
        ("/collect", "/collect"),
        ("/boards", "/boards"),
        ("/ready", "/ready"),
        ("/resume-profile", "/resume-profile"),
        ("/questionnaire", "/questionnaire"),
        ("/usage", "/usage"),
        ("/failed-tries", "/usage"),
        ("/settings", "/settings"),
        ("/settings/council", "/settings"),
        ("/settings/writer", "/settings"),
        ("/answer-key", "/answer-key"),
        ("/pilot", "/pilot"),
    ],
)
def test_every_page_has_the_menu_with_one_current_page(client, conn, data_dir, tmp_path, monkeypatch, address, current):
    settings = tmp_path / "config.yaml"
    settings.write_text(f"data_dir: {data_dir}\n", encoding="utf-8")  # the Pilot page needs a settings file
    monkeypatch.setattr(config, "CONFIG_PATH", settings)
    log_in(client)
    with conn:
        job = add_job(conn, "ML Intern")
    text = client.get(address.format(job=job)).text
    assert marked(text) == [[current], [current]]  # the rail and the phone panel
    assert '<details class="dock">' in text
    assert set(re.findall(r'icons\.svg#(i-[a-z-]+)"', text)) <= ICONS  # every icon shown is in the icon file


def test_login_and_problem_pages_have_no_menu(client):
    assert "<nav" not in client.get("/login").text
    log_in(client)
    text = client.get("/nowhere").text
    assert "Page not found." in text and "<nav" not in text
