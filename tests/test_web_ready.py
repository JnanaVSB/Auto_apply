"""Ready to Apply: which jobs, Prepare (needs confirmed tiles, never twice), Skip with Undo, and the job page's box."""

import pytest
from helpers import add_job, csrf, log_in, one
from test_tiles import RESUME_CLS
from test_web_scoring import CHOSEN, scored

from app.documents import tile_sets
from app.documents.tiles import read_tiles
from app.scoring import ready
from app.web import main


@pytest.fixture
def jobs(conn):
    with conn:
        found = {name: add_job(conn, name) for name in ("Fresh", "Older", "Low", "Anyway", "Gone", "Skipped")}
        scored(conn, found["Fresh"], score=85, reasons=["Strong RL match."])
        scored(conn, found["Older"], score=95, resume="resume-before")
        scored(conn, found["Low"], score=40)
        conn.execute("UPDATE jobs SET prepare_anyway = 1 WHERE id = ?", (found["Anyway"],))
        scored(conn, found["Gone"], score=90)
        conn.execute("UPDATE jobs SET status = 'closed' WHERE id = ?", (found["Gone"],))
        scored(conn, found["Skipped"], score=90)
        conn.execute("UPDATE jobs SET not_interested = 1 WHERE id = ?", (found["Skipped"],))
    return found


@pytest.fixture
def client(client, monkeypatch):
    monkeypatch.setattr(main, "_scoring", lambda: CHOSEN)
    log_in(client)
    return client


def confirm_tiles(conn, resume=CHOSEN.resume):
    tile_sets.confirm(conn, read_tiles(RESUME_CLS).tiles, resume)


def press(client, job_id, action="prepare", **form):
    return client.post(f"/ready/{job_id}/{action}", data=form | {"csrf_token": csrf(client)}, follow_redirects=False)


def test_only_open_ready_jobs_from_the_current_resume_are_listed(client, jobs):
    text = client.get("/ready").text
    assert ">Fresh</a>" in text and ">Anyway</a>" in text and "Strong RL match." in text and "2 jobs ready." in text
    for name in ("Older", "Low", "Gone", "Skipped"):
        assert f">{name}</a>" not in text
    assert "confirm your tiles first" in text


def test_prepare_needs_confirmed_tiles_from_the_current_resume(client, conn, jobs):
    assert press(client, jobs["Fresh"]).headers["location"] == "/ready?problem=tiles"
    confirm_tiles(conn, "resume-before")  # tiles from an older resume don't count
    assert press(client, jobs["Fresh"]).headers["location"] == "/ready?problem=tiles"
    assert one(conn, "SELECT prepare_requested_on FROM jobs WHERE id = ?", jobs["Fresh"]) == (None,)
    assert "Confirm your tiles first" in client.get("/ready?problem=tiles").text


def test_prepare_queues_a_ready_job_once(client, conn, jobs):
    confirm_tiles(conn)
    assert press(client, jobs["Fresh"]).headers["location"] == "/ready"
    first = one(conn, "SELECT prepare_requested_on FROM jobs WHERE id = ?", jobs["Fresh"])
    assert first[0] and "queued (you pressed Prepare on" in client.get("/ready").text
    assert not ready.queue(conn, jobs["Fresh"], "later")  # a second press changes nothing
    assert one(conn, "SELECT prepare_requested_on FROM jobs WHERE id = ?", jobs["Fresh"]) == first


@pytest.mark.parametrize("name", ["Older", "Low", "Gone"])
def test_a_job_that_is_not_ready_is_never_queued(client, conn, jobs, name):
    confirm_tiles(conn)
    assert press(client, jobs[name]).headers["location"] == "/ready?problem=not-ready"
    assert one(conn, "SELECT prepare_requested_on FROM jobs WHERE id = ?", jobs[name]) == (None,)


def test_skip_hides_the_job_and_undo_brings_it_back(client, conn, jobs):
    assert press(client, jobs["Fresh"], "skip", value="1").headers["location"] == f"/ready?skipped={jobs['Fresh']}"
    text = client.get(f"/ready?skipped={jobs['Fresh']}").text
    assert ">Fresh</a>" not in text and "Skipped" in text and "Undo" in text
    press(client, jobs["Fresh"], "skip", value="0")
    assert ">Fresh</a>" in client.get("/ready").text


def test_the_job_page_box_offers_prepare_only_when_it_can(client, conn, jobs):
    assert "confirm your tiles first" in client.get(f"/jobs/{jobs['Fresh']}/preparation").text
    assert "Not ready" in client.get(f"/jobs/{jobs['Older']}/preparation").text
    confirm_tiles(conn)
    assert 'action="/ready/' in client.get(f"/jobs/{jobs['Fresh']}/preparation").text
    assert press(client, jobs["Fresh"], back="job").headers["location"] == f"/jobs/{jobs['Fresh']}"
    assert "Queued: you pressed Prepare on" in client.get(f"/jobs/{jobs['Fresh']}/preparation").text
    assert 'hx-get="/jobs/' in client.get(f"/jobs/{jobs['Fresh']}").text


@pytest.mark.parametrize("path", ["/ready/999/prepare", "/ready/999/skip", "/jobs/999/preparation"])
def test_unknown_jobs_are_not_found(client, jobs, path):
    answer = client.get(path) if path.endswith("preparation") else client.post(path, data={"csrf_token": csrf(client)})
    assert answer.status_code == 404


def test_a_closed_job_is_never_queued_even_without_the_page(conn, jobs):
    assert not ready.queue(conn, jobs["Gone"], "now")
    assert one(conn, "SELECT prepare_requested_on FROM jobs WHERE id = ?", jobs["Gone"]) == (None,)
