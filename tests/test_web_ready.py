"""Ready to Apply: which jobs, Prepare (needs confirmed tiles, never twice), Skip with Undo, and the job page's box."""

import pytest
from helpers import add_job, csrf, log_in, one
from test_tiles import RESUME_CLS
from test_uploads import OTHER, form
from test_web_scoring import CHOSEN, scored

from app import config
from app.council import runs
from app.documents import tile_sets, uploads, writer_sources
from app.documents.tiles import read_tiles
from app.scoring import ready
from app.web import main, prepare, preparer


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
def writer_chosen(tmp_path, data_dir, monkeypatch):
    """A made-up settings file with a local Writer chosen."""
    path = tmp_path / "config.yaml"
    path.write_text(
        f"data_dir: {data_dir}\nai:\n"
        + "".join(
            f"  {member}:\n    model: gemma4:26b-a4b-it-q8_0\n"
            for member in ("writer", "truth", "hiring", "editor", "ats", "domain")
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_PATH", path)


@pytest.fixture
def client(client, writer_chosen, monkeypatch):
    """Logged in; `client.started` lists the jobs Prepare started the Writer on (never really run here)."""
    monkeypatch.setattr(main, "_scoring", lambda: CHOSEN)
    client.started, client.waived = [], []

    def start(conn, config, job, kind="resume", feedback="", waived=frozenset(), mode="half", automatic=False,
              writer_only=False):  # fmt: skip
        client.started.append((job, feedback or kind) if feedback or kind != "resume" else job)
        client.waived.append(waived)

    monkeypatch.setattr(prepare.flow, "start", start)
    monkeypatch.setattr(client.app.state.preparer, "wake", lambda: None)
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
    assert first[0] and client.started == [jobs["Fresh"]]  # the Writer is started (never really run here)
    assert not ready.queue(conn, jobs["Fresh"], "later")  # a second press changes nothing
    assert one(conn, "SELECT prepare_requested_on FROM jobs WHERE id = ?", jobs["Fresh"]) == first


@pytest.mark.parametrize(("name", "problem"), [("Older", "not-ready"), ("Low", "not-ready"), ("Gone", "closed")])
def test_a_job_that_is_not_ready_is_never_queued(client, conn, jobs, name, problem):
    confirm_tiles(conn)
    assert press(client, jobs[name]).headers["location"] == f"/ready?problem={problem}"
    assert one(conn, "SELECT prepare_requested_on FROM jobs WHERE id = ?", jobs[name]) == (None,)


def test_a_skipped_job_is_never_queued(client, conn, jobs):
    confirm_tiles(conn)
    assert press(client, jobs["Skipped"]).headers["location"] == "/ready?problem=skipped"
    assert one(conn, "SELECT prepare_requested_on FROM jobs WHERE id = ?", jobs["Skipped"]) == (None,)
    assert not ready.queue(conn, jobs["Skipped"], "now")  # even without the page
    assert "skipped (&#34;Not interested&#34;). To proceed anyway, press the button on the job&#39;s page" in (
        client.get("/ready?problem=skipped").text
    )


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


@pytest.mark.parametrize("number", ["9" * 5000, "²", "0", "-1"])
def test_odd_page_and_skipped_numbers_never_break_the_page(client, jobs, number):
    answer = client.get(f"/ready?page={number}&skipped={number}")
    assert answer.status_code == 200 and "Page 1 of 1" in answer.text


def test_the_ready_page_loads_the_sources_once(client, conn, jobs, monkeypatch):
    calls, offered = [], writer_sources.offered
    monkeypatch.setattr(writer_sources, "offered", lambda *args: calls.append(1) or offered(*args))
    assert "confirm first: Profile" in client.get("/ready").text and len(calls) == 1


def test_skip_hides_the_job_and_undo_brings_it_back(client, conn, jobs):
    assert press(client, jobs["Fresh"], "skip", value="1").headers["location"] == f"/ready?skipped={jobs['Fresh']}"
    text = client.get(f"/ready?skipped={jobs['Fresh']}").text
    assert ">Fresh</a>" not in text and "Skipped" in text and "Undo" in text
    press(client, jobs["Fresh"], "skip", value="0")
    assert ">Fresh</a>" in client.get("/ready").text


def test_the_job_page_box_offers_prepare_only_when_it_can(client, conn, jobs):
    assert "confirm these first: Profile" in client.get(f"/jobs/{jobs['Older']}/preparation").text  # first of all
    confirm_tiles(conn)
    assert "Not ready" in client.get(f"/jobs/{jobs['Older']}/preparation").text
    assert 'action="/ready/' in client.get(f"/jobs/{jobs['Fresh']}/preparation").text
    assert 'id="preparation"' in press(client, jobs["Fresh"], back="job").text  # the box comes back
    assert client.started == [jobs["Fresh"]] and client.waived == [frozenset()]
    assert 'hx-get="/jobs/' in client.get(f"/jobs/{jobs['Fresh']}").text


@pytest.mark.parametrize(
    "path",
    [
        *(f"/ready/{number}/{action}" for number in (999, 2**63) for action in ("prepare", "skip")),
        *(f"/jobs/{number}/{part}" for number in (999, 2**63) for part in ("preparation", "sources")),
        f"/resume-profile/files/{2**63}/remove",  # larger than the database can hold: not found, never a crash
    ],
)
def test_unknown_jobs_are_not_found(client, jobs, path):
    answer = client.get(path) if path.endswith("preparation") else client.post(path, data={"csrf_token": csrf(client)})
    assert answer.status_code == 404


def test_a_skipped_closed_or_not_ready_job_asks_first_and_can_proceed_anyway(client, conn, jobs):
    text = press(client, jobs["Skipped"], back="job", anyway="skipped").text  # unconfirmed entries: never asked
    assert "Confirm the entries of every source this job uses first" in text and client.started == []
    confirm_tiles(conn)
    text = client.get(f"/jobs/{jobs['Skipped']}/preparation").text
    assert 'hx-confirm="This job is skipped (&#34;Not interested&#34;). Press OK to proceed anyway."' in text
    assert """hx-vals='{"anyway": "skipped"}'""" in text and 'name="anyway"' not in text  # sent only after OK
    assert ">Prepare</button>" in text and ">Write cover letter<" in text
    text = client.get(f"/jobs/{jobs['Gone']}/preparation").text
    assert """hx-vals='{"anyway": "closed"}'""" in text and "No longer open." in text
    assert "hx-confirm" not in client.get(f"/jobs/{jobs['Fresh']}/preparation").text  # nothing to ask
    for name, doubt in (("Skipped", "skipped"), ("Gone", "closed"), ("Low", "not-ready")):
        text = press(client, jobs[name], back="job", anyway="closed" if doubt != "closed" else "").text
        assert "To proceed anyway, press the button" in text and jobs[name] not in client.started  # not asked
        press(client, jobs[name], back="job", anyway=doubt)
        assert client.started[-1] == jobs[name] and client.waived[-1] == {doubt}


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


@pytest.fixture
def ready_job(client, conn):
    with conn:
        job = add_job(conn, "Fresh")
        scored(conn, job, score=85)
    confirm_tiles(conn)
    return job


def test_prepare_starts_the_writer_only_when_a_version_is_needed(client, conn, ready_job):
    assert press(client, ready_job).headers["location"] == "/ready"
    assert client.started == [ready_job]
    with conn:
        conn.execute(
            "INSERT INTO preparations (job_id, kind, version, sources, writer, status, draft, created_on) VALUES "
            "(?, 'resume', 1, ?, '{}', 'done', '{}', '2026-10-30T10:00:00+00:00')",
            (ready_job, '[{"source": "profile", "set": 1}]'),
        )
    press(client, ready_job)
    assert client.started == [ready_job]  # done from the same entries: no second version
    with conn:
        conn.execute("UPDATE preparations SET status = 'by_hand', problem = 'Writer failed twice (x): check by hand.'")
    text = client.get(f"/jobs/{ready_job}/preparation").text
    assert "Version 1: Writer failed twice (x): check by hand." in text and ">Retry</button>" in text
    press(client, ready_job)
    assert client.started == [ready_job, ready_job]  # Retry: a new version


def test_a_version_made_from_older_entries_can_be_prepared_again(client, conn, ready_job):
    with conn:
        conn.execute(
            "INSERT INTO preparations (job_id, kind, version, sources, writer, status, draft, created_on) VALUES "
            "(?, 'resume', 1, '[{\"source\": \"profile\", \"set\": 99}]', '{}', 'done', '{}', 'now')", (ready_job,),
        )  # fmt: skip
    assert "was made from older entries" in client.get(f"/jobs/{ready_job}/preparation").text
    press(client, ready_job)
    assert client.started == [ready_job]


def test_prepare_without_a_writer_says_where_to_choose_one(client, conn, ready_job, tmp_path, data_dir, monkeypatch):
    path = tmp_path / "no-writer.yaml"
    path.write_text(f"data_dir: {data_dir}\n", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    assert press(client, ready_job).headers["location"] == "/ready?problem=writer"
    assert (
        client.started == []
        and "Choose the Writer&#39;s model on the Writer settings page" in client.get("/ready?problem=writer").text
    )
    assert 'href="/settings/writer"' in client.get(f"/jobs/{ready_job}/preparation").text


def test_the_box_says_what_the_writer_is_doing_and_keeps_itself_up_to_date(client, conn, ready_job, monkeypatch):
    state = client.app.state.preparer
    with conn:
        conn.execute(
            "INSERT INTO preparations (job_id, kind, version, sources, writer, status, created_on) VALUES "
            "(?, 'resume', 9, '[]', '{}', 'writing', 'now')", (ready_job,),
        )  # fmt: skip
    text = client.get(f"/jobs/{ready_job}/preparation").text
    assert "The Writer is working on it" in text and 'hx-trigger="every 3s"' in text
    with conn:
        conn.execute("DELETE FROM preparations")
        conn.execute(
            "INSERT INTO preparations (job_id, kind, version, sources, writer, status, draft, created_on) VALUES "
            "(?, 'resume', 1, ?, '{}', 'done', '{}', '2026-10-30T10:00:00+00:00')",
            (ready_job, '[{"source": "profile", "set": 1}]'),
        )
    text = client.get(f"/jobs/{ready_job}/preparation").text
    assert "Done: version 1, written 2026-10-30." in text and "every 3s" not in text
    state.notice = preparer.BUSY
    with conn:
        conn.execute("UPDATE preparations SET status = 'stopped', problem = 'Paused: today&#39;s limit.'")
    assert preparer.BUSY in client.get(f"/jobs/{ready_job}/preparation").text
    assert runs.latest(conn, ready_job)["status"] == "stopped"


def test_resume_only_needs_no_council_and_prepare_does(client, conn, ready_job, data_dir):
    config.CONFIG_PATH.write_text(f"data_dir: {data_dir}\nai:\n  writer:\n    model: gemma4:26b-a4b-it-q8_0\n",
                                  encoding="utf-8")  # fmt: skip
    text = press(client, ready_job, back="job").text
    assert "Choose the council&#39;s members in Settings first, or press Resume only" in text and client.started == []
    press(client, ready_job, back="job", writer_only="1")
    assert client.started == [ready_job]
    assert ">Resume only</button>" in client.get("/ready").text
