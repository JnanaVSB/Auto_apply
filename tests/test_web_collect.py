"""Collect now and the schedule, with a fake web, made-up jobs and a throwaway database."""

import sqlite3
import threading
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from helpers import ACME, ADDRESS, LIST, FakeWeb, board_job, csrf, entry, log_in, one, settings

from app import collect, fetch, phonebook
from app.fetch import Fetcher
from app.web import collector, main, security

pytestmark = pytest.mark.usefixtures("no_waiting")  # downloads never really wait

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


@pytest.fixture
def config(data_dir):
    """The settings every part of the dashboard reads; tests may replace them."""
    return {"config": settings(data_dir).model_copy(update={"collect_every_hours": 6, "phonebook_every_days": 0})}


@pytest.fixture
def web():
    return FakeWeb({LIST: [entry()], ACME: {"jobs": [board_job()]}})


@pytest.fixture
def client(client, config, monkeypatch):
    """Logged in, reading `config` as its settings."""
    for module in (collector, main):
        monkeypatch.setattr(module, "load_config", lambda: config["config"])
    log_in(client)
    return client


def collect_now(client):
    return client.post("/collect", data={"csrf_token": csrf(client)})


def wait_for(check):
    for _ in range(200):
        if check():
            return
        time.sleep(0.01)
    pytest.fail("timed out")


def finish(client):
    worker = client.app.state.collector
    worker._thread.join(10)
    return worker


def test_collect_now_runs_the_command_and_shows_its_summary(client, conn):
    collect_now(client)
    assert finish(client).state == "done"
    progress = client.get("/collect/progress").text
    assert "Finished." in progress and "Total: 1 new, 1 updated, 0 no longer available." in progress
    assert "hx-trigger" not in progress  # nothing running: the box stops refreshing
    assert conn.execute("SELECT title FROM jobs").fetchall() == [("ML Intern",)]
    page = client.get("/collect").text
    assert "<td>done</td><td>1</td>" in page


@pytest.fixture
def held(monkeypatch):
    """Make the collection wait until released, as a long run would."""
    release = threading.Event()

    def slow(conn, config, fetcher, out):
        out("Reading 672 company boards one at a time (about 23 minutes)...")
        release.wait(10)

    monkeypatch.setattr(collector.collect, "collect", slow)
    yield release
    release.set()


def test_only_one_collection_at_a_time_and_the_box_refreshes_while_it_runs(client, held):
    assert collect_now(client).status_code == 200
    wait_for(lambda: client.app.state.collector.lines)
    progress = client.get("/collect/progress").text
    assert 'hx-get="/collect/progress" hx-trigger="every 2s"' in progress and "Reading 672 company boards" in progress
    assert collect.BUSY in collect_now(client).text
    assert "disabled" in client.get("/collect").text
    held.set()
    assert finish(client).state == "done"


def test_a_collection_from_a_terminal_blocks_collect_now(client, data_dir):
    with closing(collect.lock(data_dir)):
        text = collect_now(client).text
    assert collect.BUSY in text
    assert client.app.state.collector.state == "idle"


def test_missing_settings_give_the_commands_message(client, config, data_dir):
    config["config"] = config["config"].model_copy(update={"roles": settings(data_dir).roles.model_copy(
        update={"simplify_categories": [], "keywords": []})})  # fmt: skip
    assert "No roles set in data/config.yaml" in collect_now(client).text


def test_no_internet_shows_one_line(client, web):
    web.down = set(fetch.ALLOWED_HOSTS)
    collect_now(client)
    worker = finish(client)
    assert worker.state == "failed"
    assert worker.lines == ["Error: No internet connection: none of the job sites could be reached. Check your "
                            "connection and try again."]  # fmt: skip


def test_closing_the_dashboard_stops_a_collection_and_keeps_finished_sources(client, conn, web, config, data_dir):
    config["config"] = settings(data_dir, ["lever:globex"])  # a board on another site

    def close_after_the_list(request):
        if request.method == "GET" and request.url.host == "boards-api.greenhouse.io":  # the Simplify list is saved
            client.app.state.collector._stop.set()
        return web(request)

    client.app.state.collector.fetcher = Fetcher(httpx.MockTransport(close_after_the_list))
    collect_now(client)
    worker = finish(client)
    assert (worker.state, worker.lines[-1]) == ("cancelled", collector.CANCELLED)
    assert conn.execute("SELECT outcome FROM collect_runs").fetchall() == [("cancelled",)]
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1  # from the Simplify list, finished first


def test_leaving_the_dashboard_stops_its_collection(data_dir, clock, config, web, monkeypatch, held):
    monkeypatch.setattr(collector, "load_config", lambda: config["config"])

    def until_stopped(conn, config, fetcher, out):
        while True:
            fetcher.get_json(LIST)

    monkeypatch.setattr(collector.collect, "collect", until_stopped)
    app = main.create_app(data_dir, security.Logins(clock=clock), lambda: Fetcher(httpx.MockTransport(web)))
    with TestClient(app, base_url=ADDRESS) as client:
        log_in(client)
        collect_now(client)
        assert app.state.collector.running
    assert (app.state.collector.state, app.state.collector.running) == ("cancelled", False)


@pytest.mark.parametrize(
    ("every", "last_started", "running", "due"),
    [
        (0, None, False, False),  # off
        (6, None, False, True),  # never collected
        (6, NOW - timedelta(hours=5), False, False),
        (6, NOW - timedelta(hours=6), False, True),
        (6, NOW - timedelta(hours=7), True, False),  # one is running
    ],
)
def test_the_schedule_is_due_only_when_on_idle_and_old_enough(data_dir, conn, every, last_started, running, due):
    if last_started:
        with conn:
            conn.execute("INSERT INTO collect_runs (started_at) VALUES (?)", (last_started.isoformat(),))
    worker = collector.Collector(data_dir, Fetcher())
    if running:
        worker._thread = threading.Thread(target=threading.Event().wait, args=(5,), daemon=True)
        worker._thread.start()
    assert worker.due(conn, every, NOW) is due


@pytest.mark.parametrize(
    ("every", "runs", "running", "due"),
    [
        (0, [], False, False),  # off
        (30, [], False, True),  # never refreshed
        (30, [(29, "done", 0)], False, False),
        (30, [(30, "done", 0)], False, True),  # a new round 30 days after the last round began
        (30, [(29, "partial", 0), (2, "partial", 1)], False, True),  # unfinished: continued after a day...
        (30, [(29, "partial", 0), (2, "done", 1)], False, False),  # ...and the clock still counts from day 29
        (30, [(5, "partial", 0), (0.5, "partial", 1)], False, False),  # a failing site: at most once a day
        (30, [(5, "paused", 0)], False, True),  # paused, cancelled or stopped: at the next chance
        (30, [(5, None, 0)], False, True),  # never finished (a power cut)
        (30, [(40, "partial", 0), (0.5, "partial", 1)], False, True),  # the round is 30 days old: a new one
        (30, [], True, False),  # something is running here
    ],
)
def test_the_phone_book_schedule_rule(data_dir, conn, every, runs, running, due):
    with conn:
        for days_ago, outcome, continues in runs:
            started = (NOW - timedelta(days=days_ago)).isoformat()
            conn.execute("INSERT INTO phonebook_runs (started_at, outcome, continues) VALUES (?, ?, ?)",
                         (started, outcome, continues))  # fmt: skip
    worker = collector.Collector(data_dir, Fetcher())
    if running:
        worker._thread = threading.Thread(target=threading.Event().wait, args=(5,), daemon=True)
        worker._thread.start()
    assert worker.refresh_due(conn, every, NOW) is due


def test_refresh_phone_book_runs_beside_the_dashboard_and_shows_its_summary(client, conn, web):
    response = client.post("/phonebook/refresh", data={"csrf_token": csrf(client)}, follow_redirects=False)
    assert response.headers["location"] == "/collect"
    worker = finish(client)
    assert (worker.task, worker.state) == ("refresh", "done")
    assert worker.lines[0] == "Simplify Summer2027-Internships: 1 boards linked, 1 new names"
    assert worker.lines[-1].startswith("Checked 3 names: 1 added")  # acme, then the same name on Lever and Ashby
    page = client.get("/collect").text
    assert "<td>partial</td>" in page  # Common Crawl is not in the fake web: tried again tomorrow
    assert one(conn, "SELECT result FROM phonebook WHERE board = 'greenhouse:acme'") == ("added",)


def test_collect_now_during_a_refresh_pauses_it_and_the_refresh_continues_afterwards(
    client, conn, web, data_dir, monkeypatch
):
    monkeypatch.setattr(collect, "PAUSE_POLL_SECONDS", 0.01)
    worker, pressed = client.app.state.collector, []

    def collect_now_once_the_refresh_reads_lists(request):
        if worker.task == "refresh" and str(request.url) == LIST and not pressed:
            pressed.append(worker.start())  # what "Collect now" does
        return web(request)

    worker.fetcher = Fetcher(httpx.MockTransport(collect_now_once_the_refresh_reads_lists))
    client.post("/phonebook/refresh", data={"csrf_token": csrf(client)})
    finish(client)
    assert (pressed, worker.task, worker.state) == ([None], "refresh", "done")  # accepted, not refused
    lines = worker.lines
    paused, resumed = lines.index(phonebook.PAUSED), lines.index(collector.RESUMED)
    assert any(line.startswith("Total:") for line in lines[paused:resumed])  # the collection ran in between
    assert lines[-1].startswith("Checked ")  # then the refresh finished
    assert conn.execute("SELECT outcome, continues FROM phonebook_runs ORDER BY id").fetchall() == [
        ("paused", 0), ("partial", 1)]  # fmt: skip
    assert one(conn, "SELECT count(*) FROM collect_runs") == (1,)
    assert not (data_dir / collect.PAUSE_FILE).exists()


def test_a_collection_is_due_even_while_a_refresh_runs(data_dir, conn):
    worker = collector.Collector(data_dir, Fetcher())
    worker._thread = threading.Thread(target=threading.Event().wait, args=(5,), daemon=True)
    worker._thread.start()
    worker.task = "refresh"
    assert worker.due(conn, 6, NOW) is True
    worker.collection_wanted = True  # already asked for: not twice
    assert worker.due(conn, 6, NOW) is False


def test_the_boards_page_says_when_a_refresh_runs_or_is_paused(client):
    worker, finished = client.app.state.collector, threading.Event()
    worker._thread = threading.Thread(target=finished.wait, args=(5,), daemon=True)
    worker._thread.start()
    worker.task = "refresh"
    assert "A phone-book refresh is running" in client.get("/boards").text
    worker.task, worker.refresh_paused = "collect", True
    assert "A phone-book refresh is paused for a collection" in client.get("/boards").text
    finished.set()


def test_the_dashboard_uses_one_downloader_for_everything(data_dir, clock, web):
    made = []

    def new_fetcher():
        made.append(Fetcher(httpx.MockTransport(web)))
        return made[-1]

    app = main.create_app(data_dir, security.Logins(clock=clock), new_fetcher)
    with TestClient(app, base_url=ADDRESS) as client:
        log_in(client)
        client.post("/add/company", data={"name": "Acme", "csrf_token": csrf(client)})
        client.post("/add/board", data={"board": "greenhouse:acme", "csrf_token": csrf(client)})
    assert len(made) == 1  # so "at most N requests per second to a site" counts every request


def test_a_refresh_waits_for_a_collection_from_a_terminal(client, data_dir):
    with closing(collect.lock(data_dir)):
        assert collect.BUSY in client.post("/phonebook/refresh", data={"csrf_token": csrf(client)}).text


def test_the_schedule_refreshes_the_phone_book_after_collecting(client, conn, config):
    config["config"] = config["config"].model_copy(update={"phonebook_every_days": 30})
    worker = client.app.state.collector
    worker.tick(conn)  # both due: the collection goes first
    assert (finish(client).task, worker.state) == ("collect", "done")
    worker.tick(conn)
    assert (finish(client).task, worker.state) == ("refresh", "done")
    worker.tick(conn)  # both just ran
    assert not worker.running


def test_the_schedule_starts_a_collection_when_due(client, conn, config):
    worker = client.app.state.collector
    worker.tick(conn)  # never collected, every 6 hours: due
    assert finish(client).state == "done"
    worker.tick(conn)  # just collected: not due
    assert not worker.running
    config["config"] = config["config"].model_copy(update={"collect_every_hours": 0})
    with conn:
        conn.execute("UPDATE collect_runs SET started_at = '2020-01-01T00:00:00+00:00'")
    worker.tick(conn)  # off
    assert not worker.running


def test_the_collect_page_shows_the_schedule_and_last_runs(client, conn, config):
    with conn:
        conn.execute(
            "INSERT INTO collect_runs (started_at, finished_at, outcome, new_jobs, updated_jobs, closed_jobs, "
            "failures) VALUES ('2026-10-21T08:30:00+00:00', '2026-10-21T08:50:00+00:00', 'done', 552, 1192, 366, ?)",
            ('["lever: no answer", "needs investigation: acme"]',),
        )  # fmt: skip
        conn.execute("INSERT INTO collect_runs (started_at) VALUES ('2026-10-22T09:00:00+00:00')")
    page = client.get("/collect").text
    assert "Every 6 hours while the dashboard is open" in page
    assert "<td>2026-10-21 08:30</td><td>done</td><td>552</td>" in page and "<td>1,192</td>" in page
    assert "not finished (running, or interrupted)" in page
    config["config"] = config["config"].model_copy(update={"collect_every_hours": 0})
    assert "Off. Turn it on on the" in client.get("/collect").text


def test_collect_now_needs_the_anti_forgery_token(client):
    assert client.post("/collect").status_code == 403
    assert client.app.state.collector._thread is None


def test_an_unexpected_lock_error_never_freezes_the_dashboard(client, monkeypatch):
    def no_lock_file(data_dir):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(collector.collect, "lock", no_lock_file)
    text = collect_now(client).text
    assert "The collection lock file could not be created (unable to open database file)." in text
    assert client.app.state.collector.state == "idle"


def test_collect_now_stops_waiting_after_a_time_limit(client, monkeypatch):
    def slow_lock(data_dir):
        time.sleep(1)
        raise collect.CollectError(collect.BUSY)

    monkeypatch.setattr(collector, "START_WAIT_SECONDS", 0.1)
    monkeypatch.setattr(collector.collect, "lock", slow_lock)
    assert "did not answer in time" in collect_now(client).text


def test_reloading_after_collect_now_never_starts_another(client, conn):
    response = client.post("/collect", data={"csrf_token": csrf(client)}, follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/collect")
    finish(client)
    client.get("/collect")
    client.get("/collect")  # a reload is a plain page view
    assert conn.execute("SELECT count(*) FROM collect_runs").fetchone()[0] == 1
