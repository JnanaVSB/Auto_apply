"""Settings → USAJobs key: a key and e-mail that only ever go into the keychain. Fake keychain, made-up values."""

import logging
from contextlib import closing
from datetime import date

import httpx
import pytest
from helpers import FakeWeb, add_job, csrf, log_in, one, settings

from app import collect, fetch, jobs, keychain, tracker
from app.config import SourceSettings
from app.sources import usajobs
from app.web import usajobs_settings

pytestmark = pytest.mark.usefixtures("no_waiting")  # downloads never really wait

KEY, MAIL = "fake-key-1234", "sam.fake@example.com"
PAGE = usajobs_settings.PAGE


@pytest.fixture
def page(client):
    log_in(client)
    return client


def post(client, address=PAGE, **form):
    return client.post(address, data=form | {"csrf_token": csrf(client)})


def saved():
    return keychain.get_secret(usajobs.KEY), keychain.get_secret(usajobs.EMAIL)


def test_a_saved_key_and_e_mail_are_said_to_be_saved_and_never_shown(page):
    text = post(page, key=KEY, email=MAIL).text
    assert saved() == (KEY, MAIL)
    for shown in (text, page.get(PAGE).text):
        assert "Key: saved" in shown and "E-mail: saved" in shown
        assert KEY not in shown and MAIL not in shown


def test_the_page_says_what_the_e_mail_is_for(page):
    assert "USAJobs asks for your e-mail with every request; the app sends it to USAJobs only" in page.get(PAGE).text


def test_with_nothing_saved_the_page_says_so(page):
    text = page.get(PAGE).text
    assert "Key: not saved" in text and "E-mail: not saved" in text


def test_one_can_be_changed_and_the_other_stays(page):
    post(page, key=KEY, email=MAIL)
    post(page, key="fake-key-5678", email="")
    assert saved() == ("fake-key-5678", MAIL)


@pytest.mark.parametrize(
    "form",
    [
        {"key": "has a space", "email": MAIL},
        {"key": "line\nbreak", "email": MAIL},
        {"key": "x" * 501, "email": MAIL},
        {"key": KEY, "email": "sam\n.fake@example.com"},
        {"key": KEY, "email": "sam@fake@example.com"},
        {"key": KEY, "email": "no-at-sign.example.com"},
        {"key": KEY, "email": "sam fake@example.com"},
        {"key": KEY, "email": "x" * 250 + "@example.com"},
        {"key": KEY, "email": "sám@example.com"},
        {"key": KEY, "email": "sam@exámple.com"},
        {"key": KEY, "email": "sam\0@example.com"},
        {"key": KEY, "email": "sam\t@example.com"},
        {"key": "", "email": ""},
    ],
)
def test_a_bad_value_saves_nothing_and_nothing_typed_comes_back(page, form):
    response = post(page, **form)
    assert response.status_code == 400
    assert saved() == (None, None)
    assert not [value for value in form.values() if value and value in response.text]


def test_no_form_token_is_refused(page):
    assert page.post(PAGE, data={"key": KEY, "email": MAIL}).status_code == 403
    assert page.post(PAGE + "/remove").status_code == 403
    assert saved() == (None, None)


def test_the_key_reaches_no_file_and_no_log(page, data_dir, caplog):
    with caplog.at_level(logging.DEBUG):
        post(page, key=KEY, email=MAIL)
    assert KEY not in caplog.text and MAIL not in caplog.text
    files = [path for path in data_dir.rglob("*") if path.is_file()]
    assert not [path for path in files if KEY.encode() in path.read_bytes() or MAIL.encode() in path.read_bytes()]


def test_remove_deletes_both_and_removing_nothing_is_fine(page):
    post(page, key=KEY, email=MAIL)
    assert "Removed." in post(page, PAGE + "/remove").text
    assert saved() == (None, None)
    assert post(page, PAGE + "/remove").status_code == 200


def test_a_failing_keychain_is_said_honestly(page, monkeypatch):
    def locked(*_):
        raise keychain.KeychainError("No secure OS keychain available (found: none).")

    monkeypatch.setattr(usajobs_settings, "get_secret", locked)
    monkeypatch.setattr(usajobs_settings, "set_secret", locked)
    monkeypatch.setattr(usajobs_settings, "delete_secret", locked)
    assert "No secure OS keychain" in page.get(PAGE).text
    assert "No secure OS keychain" in post(page, key=KEY, email=MAIL).text
    assert "No secure OS keychain" in post(page, PAGE + "/remove").text


def collect_with(conn, data_dir, pages, feeds):
    web, lines = FakeWeb(pages), []
    config = settings(data_dir).model_copy(update={"sources": SourceSettings(feeds=list(feeds))})
    with closing(fetch.Fetcher(httpx.MockTransport(web))) as fetcher:
        collect.collect(conn, config, fetcher, lines.append)
    return lines, web


def test_a_key_removed_while_ticked_skips_it_and_closes_nothing(conn, data_dir):
    with conn:
        add_job(conn, "ML Engineer", ["feed:usajobs"])
    lines, _ = collect_with(conn, data_dir, {}, ["usajobs"])
    assert f"USAJobs: {usajobs.NEEDS_KEY}" in lines
    assert one(conn, "SELECT status FROM jobs") == ("open",)


def test_with_the_default_settings_nothing_is_asked_and_nothing_is_said(conn, data_dir):
    lines, web = collect_with(conn, data_dir, {}, SourceSettings().feeds)
    assert "data.usajobs.gov" not in {request.url.host for request in web.requests}
    assert not [line for line in lines if "USAJobs" in line]


def test_unticked_after_use_its_jobs_close_the_tracker_is_left_and_the_key_stays(conn, data_dir):
    keychain.set_secret(usajobs.KEY, KEY)
    keychain.set_secret(usajobs.EMAIL, MAIL)
    with conn:
        add_job(conn, "ML Engineer", ["feed:usajobs"])
        kept = add_job(conn, "ML Intern", ["feed:usajobs"])
        tracker.move(conn, kept, "ready", "applied", date(2026, 11, 4))
    collect_with(conn, data_dir, {}, [])
    assert conn.execute("SELECT title, status_reason FROM jobs ORDER BY id").fetchall() == [
        ("ML Engineer", jobs.FEED_OFF), ("ML Intern", None)]  # fmt: skip
    assert saved() == (KEY, MAIL)
