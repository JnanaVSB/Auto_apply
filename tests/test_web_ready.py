"""Ready to Apply: which jobs, Prepare (needs confirmed tiles, never twice), Skip with Undo, and the job page's box."""

import pytest
from helpers import add_job, csrf, log_in, one
from test_tiles import RESUME_CLS
from test_uploads import OTHER, form
from test_web_scoring import CHOSEN, scored

from app.documents import tile_sets, uploads, writer_sources
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
    assert "confirm first: Profile" in text and "uses: Profile" in text


def test_prepare_needs_confirmed_tiles_from_the_current_resume(client, conn, jobs):
    assert press(client, jobs["Fresh"]).headers["location"] == "/ready?problem=tiles"
    confirm_tiles(conn, "resume-before")  # tiles from an older resume don't count
    assert press(client, jobs["Fresh"]).headers["location"] == "/ready?problem=tiles"
    assert one(conn, "SELECT prepare_requested_on FROM jobs WHERE id = ?", jobs["Fresh"]) == (None,)
    assert "Confirm the entries of every source this job uses first" in client.get("/ready?problem=tiles").text


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


def test_a_skipped_job_is_never_queued(client, conn, jobs):
    confirm_tiles(conn)
    assert press(client, jobs["Skipped"]).headers["location"] == "/ready?problem=skipped"
    assert one(conn, "SELECT prepare_requested_on FROM jobs WHERE id = ?", jobs["Skipped"]) == (None,)
    assert not ready.queue(conn, jobs["Skipped"], "now")  # even without the page
    assert "You skipped this job: turn off" in client.get("/ready?problem=skipped").text


def test_skipping_a_queued_job_takes_it_out_of_the_queue_and_undo_puts_it_back(client, conn, jobs):
    confirm_tiles(conn)
    press(client, jobs["Fresh"])
    press(client, jobs["Anyway"])
    assert ready.queued(conn) == [jobs["Fresh"], jobs["Anyway"]]
    press(client, jobs["Fresh"], "skip", value="1")
    assert ready.queued(conn) == [jobs["Anyway"]]
    assert "kept out of the queue. Your Prepare press of" in client.get(f"/jobs/{jobs['Fresh']}/preparation").text
    press(client, jobs["Fresh"], "skip", value="0")
    assert ready.queued(conn) == [jobs["Fresh"], jobs["Anyway"]]


def test_skip_hides_the_job_and_undo_brings_it_back(client, conn, jobs):
    assert press(client, jobs["Fresh"], "skip", value="1").headers["location"] == f"/ready?skipped={jobs['Fresh']}"
    text = client.get(f"/ready?skipped={jobs['Fresh']}").text
    assert ">Fresh</a>" not in text and "Skipped" in text and "Undo" in text
    press(client, jobs["Fresh"], "skip", value="0")
    assert ">Fresh</a>" in client.get("/ready").text


def test_the_job_page_box_offers_prepare_only_when_it_can(client, conn, jobs):
    assert "confirm these first: Profile" in client.get(f"/jobs/{jobs['Fresh']}/preparation").text
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
    with conn:  # queued before it closed: out of the queue
        conn.execute("UPDATE jobs SET prepare_requested_on = 'earlier' WHERE id = ?", (jobs["Gone"],))
    assert ready.queued(conn) == []


def test_a_job_can_use_its_own_sources_and_prepare_needs_them_all_confirmed(client, conn, jobs):
    confirm_tiles(conn)
    uploads.upload(conn, form(("robotics-cv.tex", OTHER)))
    file = uploads.offered(conn)[0]
    data = {"source.profile": "1", f"source.{file.id}": "1", "csrf_token": csrf(client)}
    client.post(f"/jobs/{jobs['Fresh']}/sources", data=data)
    assert writer_sources.of_job(conn, jobs["Fresh"]) == (["profile", file.id], True)
    assert "uses: Profile + robotics-cv" in client.get("/ready").text
    assert "confirm these first: robotics-cv" in client.get(f"/jobs/{jobs['Fresh']}/preparation").text
    assert press(client, jobs["Fresh"]).headers["location"] == "/ready?problem=tiles"  # the file isn't confirmed
    tile_sets.confirm(conn, read_tiles(OTHER).tiles, file.fingerprint, file.id)
    assert press(client, jobs["Fresh"]).headers["location"] == "/ready"
    assert press(client, jobs["Anyway"]).headers["location"] == "/ready"  # the default: the Profile, confirmed
    back = {"default": "1", f"source.{file.id}": "1", "csrf_token": csrf(client)}  # "Use my default" wins
    client.post(f"/jobs/{jobs['Fresh']}/sources", data=back)
    assert writer_sources.of_job(conn, jobs["Fresh"]) == (["profile"], False)


def test_the_default_choice_is_saved_on_the_resume_profile_page(client, conn, data_dir):
    (data_dir / "resume.tex").write_text(RESUME_CLS, encoding="utf-8")
    assert "Tick at least one" in client.post("/resume-profile/sources", data={"csrf_token": csrf(client)}).text
    client.post("/resume-profile/sources", data={"source.profile": "1", "source.99": "1", "csrf_token": csrf(client)})
    assert writer_sources.default(conn) == ["profile"]  # a file that isn't offered is never chosen
    assert 'name="source.profile" value="1" checked' in client.get("/resume-profile").text
