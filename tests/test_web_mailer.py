"""The Gmail sync beside the dashboard: when it is due, one pass at a time, and plain words when it can't run."""

import logging
import threading
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from helpers import add_job, one
from test_gmail_client import FakeGoogle
from test_tracker import version

from app import config, tracker
from app.gmail import client, sync
from app.keychain import set_secret
from app.web import mailer as mailer_module
from app.web.mailer import Mailer


@pytest.fixture
def settings(tmp_path, data_dir, monkeypatch):
    """A made-up settings file; returns a function that rewrites it with these Gmail minutes."""

    def write(minutes=30):
        path = tmp_path / "config.yaml"
        path.write_text(f"data_dir: {data_dir}\ngmail_every_minutes: {minutes}\n", encoding="utf-8")
        monkeypatch.setattr(config, "CONFIG_PATH", path)

    write()
    return write


@pytest.fixture
def connected():
    for name, value in ((client.CLIENT_ID, "id.apps.googleusercontent.com"), (client.CLIENT_SECRET, "made-up"),
                        (client.REFRESH, "made-up-lasting")):  # fmt: skip
        set_secret(name, value)


@pytest.fixture
def mailer(data_dir, monkeypatch):
    found = Mailer(data_dir)
    found.woken = 0
    monkeypatch.setattr(found, "wake", lambda: setattr(found, "woken", found.woken + 1))
    return found


def synced(conn, minutes_ago):
    at = (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    with conn:
        conn.execute("INSERT OR REPLACE INTO gmail_sync (id, last_sync_on) VALUES (1, ?)", (at,))


@pytest.mark.parametrize("minutes, ago, woken", [(0, None, 0), (30, None, 1), (30, 31, 1), (30, 29, 0), (5, 6, 1)])
def test_a_pass_is_due_after_the_set_minutes_and_never_when_off(conn, settings, connected, mailer, minutes, ago, woken):
    settings(minutes)
    if ago is not None:
        synced(conn, ago)
    mailer.tick(conn)
    assert mailer.woken == woken


def test_nothing_is_due_without_a_connected_gmail(conn, settings, mailer):
    mailer.tick(conn)
    assert mailer.woken == 0


def test_a_pass_reads_the_fake_gmail_and_moves_a_card(conn, data_dir, settings, connected):
    with conn:
        acme = add_job(conn, "ML Intern", company="Acme")
        version(conn, acme)
    fake = FakeGoogle({
        "/profile": [{"emailAddress": "jordan@example.com", "historyId": "5"}],
        "/messages": [{"messages": [{"id": "m1"}]}],
        "/messages/m1": [{"id": "m1", "threadId": "t1", "internalDate": "1793790000000", "snippet": "",
                          "payload": {"headers": [{"name": "From", "value": "Acme <no-reply@greenhouse.io>"},
                                                  {"name": "Subject", "value": "Thank you for applying to Acme"}]}}],
    })  # fmt: skip
    found = Mailer(data_dir, httpx.MockTransport(fake))
    found._pass()
    assert found.notice == "Read 1 new e-mail: 1 card moved, 0 to check."
    assert tracker.stage(conn, acme) == "applied" and one(conn, "SELECT history_id FROM gmail_sync")[0] == "5"


def test_a_chosen_period_is_read_without_moving_the_sync_position(conn, data_dir, settings, connected):
    found = Mailer(data_dir, httpx.MockTransport(fake := FakeGoogle({"/messages": [{}]})))
    found._pass((date(2026, 10, 1), date(2026, 10, 31)))
    assert found.period_notice == "Read 0 new e-mails from 2026-10-01 to 2026-10-31: 0 cards moved, 0 to check."
    assert found.notice is None and found.tried is None and fake.calls("/profile") == 0
    assert one(conn, "SELECT count(*) FROM gmail_sync")[0] == 0


def test_a_period_and_a_sync_asked_together_both_run_the_period_first(data_dir, monkeypatch):
    found, ran = Mailer(data_dir), []
    monkeypatch.setattr(found, "_words", lambda period: ran.append(period) or "Read 0 new e-mails.")
    found._again, found.period = True, (date(2026, 10, 1), date(2026, 10, 31))
    found._passes()
    assert ran == [(date(2026, 10, 1), date(2026, 10, 31)), None]
    assert found.notice == found.period_notice == "Read 0 new e-mails." and found._thread is None


def test_a_period_blocked_by_another_dashboard_asks_to_press_again(data_dir, settings, connected, monkeypatch):
    def busy(*_):
        raise mailer_module.collect.CollectError(mailer_module.BUSY)

    monkeypatch.setattr(mailer_module.collect, "lock", busy)
    found = Mailer(data_dir)
    found._pass((date(2026, 10, 1), date(2026, 10, 31)))
    found._pass()
    assert found.period_notice == mailer_module.PERIOD_BUSY and found.notice == mailer_module.BUSY


def test_a_refused_sign_in_is_said_plainly(conn, data_dir, settings, connected):
    fake = FakeGoogle({"/token": [httpx.Response(400, json={"error": "invalid_grant"})], "/profile": [{}]})
    found = Mailer(data_dir, httpx.MockTransport(fake))
    found._pass()
    assert "connecting again" in found.notice


def test_without_a_sign_in_nothing_is_read(data_dir, settings):
    found = Mailer(data_dir)
    found._pass()
    assert found.notice == mailer_module.NOT_CONNECTED


def test_an_unexpected_error_logs_its_type_only(data_dir, settings, connected, monkeypatch, caplog):
    def broken(*_):
        raise RuntimeError("Dear Jordan Example, your code is 4321")

    monkeypatch.setattr(sync, "run", broken)
    found = Mailer(data_dir)
    with caplog.at_level(logging.ERROR, logger="app.web"):
        found._pass()
    assert found.notice == "The Gmail sync stopped on an unexpected error; the next sync tries again."
    assert "RuntimeError" in caplog.text and "Jordan" not in caplog.text and "4321" not in caplog.text


def test_presses_while_a_pass_runs_give_one_more_pass_never_two_at_once(data_dir):
    found, started, release = Mailer(data_dir), threading.Event(), threading.Event()
    running, most, passes = [0], [0], [0]

    def one_pass():
        running[0] += 1
        most[0], passes[0] = max(most[0], running[0]), passes[0] + 1
        started.set()
        release.wait(5)
        running[0] -= 1

    found._pass = one_pass
    found.wake()
    assert started.wait(5)
    found.wake()  # two presses while the first pass runs
    found.wake()
    release.set()
    for _ in range(250):
        if found._thread is None:
            break
        threading.Event().wait(0.02)
    assert (passes[0], most[0]) == (2, 1)


def test_a_pass_that_just_failed_is_not_tried_again_before_the_set_minutes(conn, settings, connected, mailer):
    synced(conn, 120)  # the last good sync, two hours ago
    mailer.tried = datetime.now(UTC) - timedelta(minutes=10)  # a try that failed ten minutes ago
    mailer.tick(conn)
    assert mailer.woken == 0
    mailer.tried -= timedelta(minutes=25)
    mailer.tick(conn)
    assert mailer.woken == 1
