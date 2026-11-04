"""Scoring beside the dashboard: Score now, Score these, automatic scoring and the Settings section. The scoring run
itself is replaced by a stand-in here; the real one is tested in test_queue.py."""

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
import yaml
from helpers import add_job, csrf, log_in, settings
from test_web_scoring import CHOSEN, scored

from app import config as settings_file
from app import llm, score
from app.config import ScoringSettings
from app.resume import RESUME_FILE
from app.scoring import queue
from app.web import main, scorer

FAKE_RESUME = (
    rb"\begin{document}" + b"Made-up resume about robots and reinforcement learning. " * 10 + rb"\end{document}"
)


class FakeRun:
    """Stands in for queue.run: records what it was asked, says one line, and can wait to be released."""

    def __init__(self, error=None, hold=False):
        self.calls, self.error = [], error
        self.release = threading.Event()
        if not hold:
            self.release.set()

    def __call__(self, conn, config, checker, setup, resume, *, job_ids=None, stop, out):
        self.calls.append({"job_ids": job_ids, "model": config.ai.evaluator.model})
        out("AI scoring with the stand-in...")
        while not self.release.wait(0.01):
            stop()
        if self.error:
            raise self.error


@pytest.fixture
def run(monkeypatch, data_dir):
    fake = FakeRun()
    monkeypatch.setattr(scorer.queue, "run", fake)
    monkeypatch.setattr(score, "prepare", lambda config, fetcher, out: ("checker", "setup", "resume"))
    monkeypatch.setattr(scorer, "load_config", lambda: settings(data_dir))
    return fake


def wait_until_idle(app_scorer):
    for _ in range(300):
        if not app_scorer.running:
            return
        time.sleep(0.01)
    raise AssertionError("scoring did not finish")


@pytest.fixture
def client(client, monkeypatch, run):
    monkeypatch.setattr(main, "_scoring", lambda: CHOSEN)
    monkeypatch.setattr(main.ollama, "status", lambda role: (True, "Ollama is running and the model m is downloaded."))
    log_in(client)
    return client


def app_scorer(client):
    return client.app.state.scorer


def test_score_now_runs_beside_the_dashboard_and_shows_its_lines(client, run, data_dir):
    reply = client.post("/score", data={"csrf_token": csrf(client)}, follow_redirects=False)
    assert reply.headers["location"] == "/collect#scoring"
    wait_until_idle(app_scorer(client))
    text = client.get("/collect").text
    assert "AI scoring with the stand-in..." in text and "Finished." in text
    assert run.calls == [{"job_ids": None, "model": settings(data_dir).ai.evaluator.model}]


def test_only_one_scoring_run_at_a_time(client, run):
    run.release.clear()
    client.post("/score", data={"csrf_token": csrf(client)})
    assert score.BUSY in client.post("/score", data={"csrf_token": csrf(client)}).text  # shown once, after the redirect
    assert 'hx-get="/score/progress"' in client.get("/collect").text  # the box refreshes while it runs
    run.release.set()
    wait_until_idle(app_scorer(client))


def test_score_these_takes_the_first_jobs_of_the_list_in_its_order(client, conn, run, monkeypatch):
    monkeypatch.setattr(main, "SCORE_THESE", 2)
    monkeypatch.setitem(main.templates.env.globals, "SCORE_THESE", 2)
    with conn:
        jobs = [add_job(conn, f"Job {number}", date_posted=f"2026-10-0{number}") for number in range(1, 5)]
        for job in jobs:
            scored(conn, job, passed=0)
    page = client.get("/?f=1&status=open&sort=posted").text
    assert "Score these 2 jobs" in page and "the first 2 in this order" in page
    client.post("/score/these", data={"csrf_token": csrf(client), "back": "/?f=1&status=open&sort=posted"})
    wait_until_idle(app_scorer(client))
    assert run.calls[-1]["job_ids"] == [jobs[3], jobs[2]]  # newest posted first


def test_score_these_with_nothing_matching_says_so(client, run):
    text = client.post("/score/these", data={"csrf_token": csrf(client), "back": "/?f=1&q=nothing"}).text
    assert "No jobs to score." in text and run.calls == []


def test_score_this_job(client, conn, run):
    with conn:
        job = add_job(conn, "ML Intern")
    assert "Score this job" in client.get(f"/jobs/{job}").text
    client.post(f"/jobs/{job}/score", data={"csrf_token": csrf(client)})
    wait_until_idle(app_scorer(client))
    assert run.calls[-1]["job_ids"] == [job]
    assert client.post("/jobs/999999/score", data={"csrf_token": csrf(client)}).status_code == 404


def test_a_failed_run_says_why_and_pauses_automatic_scoring(client, run, data_dir, monkeypatch):
    monkeypatch.setattr(main, "load_config", lambda: settings(data_dir))
    run.error = llm.StopScoring("Ollama isn't running: start it with `ollama serve`.")
    client.post("/score", data={"csrf_token": csrf(client)})
    wait_until_idle(app_scorer(client))
    text = client.get("/collect").text
    assert "Error: Ollama isn&#39;t running: start it with `ollama serve`." in text and "Could not finish." in text
    assert "waits 30 minutes before trying again" in text
    assert app_scorer(client).failed_at is not None


def test_ollama_text_in_a_stopped_run_is_shown_as_plain_text(client, run):
    run.error = llm.StopScoring("Ollama refused the request (status 500: <script>alert(1)</script>).")
    client.post("/score", data={"csrf_token": csrf(client)})
    wait_until_idle(app_scorer(client))
    text = client.get("/collect").text
    assert "status 500: &lt;script&gt;alert(1)&lt;/script&gt;" in text and "<script>alert" not in text


def test_closing_the_dashboard_stops_scoring_and_keeps_what_is_done(client, run):
    run.release.clear()
    client.post("/score", data={"csrf_token": csrf(client)})
    app_scorer(client).stop()
    assert app_scorer(client).state == "stopped" and scorer.STOPPED in app_scorer(client).lines


@pytest.fixture
def ready(data_dir, conn):
    """A resume, and one new job that needs the quick check: there is something to score."""
    (data_dir / RESUME_FILE).write_bytes(FAKE_RESUME)
    with conn:
        add_job(conn, "RL Intern", date_posted="2026-10-27")
    return settings(data_dir)


NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)
COUNCIL = """INSERT INTO preparations (job_id, kind, version, sources, writer, council, status, created_on)
    VALUES (1, 'resume', 1, '[]', '{{"service": "anthropic"}}', '{{"members": {{"truth": {{"service": "{}"}}}}}}',
    'writing', 'now')"""


@pytest.mark.parametrize(
    ("change", "due"),
    [
        (lambda s, c, conf: None, True),
        (lambda s, c, conf: setattr(s, "failed_at", NOW - timedelta(minutes=10)), False),
        (lambda s, c, conf: setattr(s, "failed_at", NOW - timedelta(minutes=31)), True),
        (
            lambda s, c, conf: c.execute(
                "INSERT INTO collect_runs (started_at) VALUES (?)", (datetime.now(UTC).isoformat(timespec="seconds"),)
            ),
            False,
        ),  # fmt: skip
        (lambda s, c, conf: c.execute("INSERT INTO scoring_days VALUES (?, 7200)", (queue.today(),)), False),
        (lambda s, c, conf: (conf.data_dir / RESUME_FILE).unlink(), False),
        (lambda s, c, conf: c.execute("UPDATE jobs SET status = 'no longer available'"), False),  # nothing to do
        (lambda s, c, conf: c.execute(COUNCIL.format("ollama")), False),  # the council's laptop work goes first
        (lambda s, c, conf: c.execute(COUNCIL.format("anthropic")), True),  # cloud work doesn't use the card
    ],
)
def test_automatic_scoring_starts_only_when_it_has_work_and_the_moment_is_right(data_dir, conn, ready, change, due):
    runner = scorer.Scorer(data_dir, fetcher=None)
    with conn:
        change(runner, conn, ready)
    assert runner.due(conn, ready, NOW) is due


def test_automatic_scoring_can_be_switched_off(data_dir, conn, ready):
    off = ready.model_copy(update={"scoring": ScoringSettings(automatic=False)})
    assert scorer.Scorer(data_dir, fetcher=None).due(conn, off, NOW) is False


def test_the_minute_check_starts_scoring_when_due(client, conn, run, ready, monkeypatch):
    monkeypatch.setattr(scorer, "load_config", lambda: ready)
    app_scorer(client).tick(conn)
    wait_until_idle(app_scorer(client))
    assert len(run.calls) == 1


@pytest.fixture
def settings_path(tmp_path, data_dir, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(
        f"data_dir: {data_dir}\nai:\n  evaluator:\n    model: qwen3.6:35b-a3b\n    key_name: kept-name\n", "utf-8"
    )
    monkeypatch.setattr(settings_file, "CONFIG_PATH", path)
    return path


def scoring_form(**changes):
    values = {
        "simplify_categories": "", "keywords": "RL", "extra_boards": "", "phonebook_lists": "",
        "collect_every_hours": "0", "phonebook_every_days": "30", "tracker_stale_days": "21",
        "gmail_lookback_days": "60", "gmail_every_minutes": "30",
        "requests_per_second": "1",
        "automatic": "off", "minutes_per_day": "90", "ready_threshold": "75", "meaning_threshold": "0.55",
        "skills_threshold": "0.2", "extra_skills": "MuJoCo\nCARLA", "ai_model": "gemma4:26b-a4b-it-qat",
        "ai_thinking": "low", "ai_context": "8192",
    }  # fmt: skip
    return values | changes


def test_the_settings_page_shows_scoring_and_the_ollama_status(client, settings_path):
    text = client.get("/settings").text
    assert "Ollama is running and the model m is downloaded." in text
    assert 'name="minutes_per_day" type="number" min="1" max="1440" step="1" value="120"' in text  # the default
    assert 'name="ai_model" maxlength="100" value="qwen3.6:35b-a3b"' in text


def test_the_settings_page_never_asks_a_cloud_service(client, settings_path, monkeypatch):
    settings_path.write_text(
        settings_path.read_text(encoding="utf-8").replace("    model:", "    service: anthropic\n    model:"), "utf-8"
    )
    monkeypatch.setattr(main.ollama, "status", lambda role: pytest.fail("asked a cloud service"))
    assert "set ai.evaluator.service to ollama" in client.get("/settings").text


def test_saving_scoring_settings_keeps_what_the_form_does_not_show(client, settings_path):
    text = client.post("/settings", data=scoring_form() | {"csrf_token": csrf(client)}).text
    assert "Saved." in text
    saved = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
    assert saved["scoring"] == {
        "automatic": False, "minutes_per_day": 90, "ready_threshold": 75, "meaning_threshold": 0.55,
        "skills_threshold": 0.2, "extra_skills": ["MuJoCo", "CARLA"],
    }  # fmt: skip
    assert saved["ai"]["evaluator"] == {"model": "gemma4:26b-a4b-it-qat", "key_name": "kept-name", "thinking": "low",
                                        "context": 8192}  # fmt: skip


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"meaning_threshold": "1.5"}, "scoring.meaning_threshold"),
        ({"minutes_per_day": "0"}, "scoring.minutes_per_day"),
        ({"ai_model": "rm -rf /"}, "ai.evaluator.model"),
        ({"ai_thinking": "max"}, "ai.evaluator.thinking"),
    ],
)
def test_bad_scoring_settings_are_refused_and_nothing_changes(client, settings_path, changes, problem):
    before = settings_path.read_bytes()
    text = client.post("/settings", data=scoring_form(**changes) | {"csrf_token": csrf(client)}).text
    assert "Not saved" in text and problem in text
    assert settings_path.read_bytes() == before


def test_the_stop_button_stops_scoring_and_the_next_run_is_not_stopped(client, run):
    run.release.clear()
    client.post("/score", data={"csrf_token": csrf(client)})
    assert "Stop scoring" in client.get("/score/progress").text
    reply = client.post("/score/stop", data={"csrf_token": csrf(client)}, follow_redirects=False)
    assert reply.headers["location"] == "/collect#scoring"
    wait_until_idle(app_scorer(client))
    assert app_scorer(client).state == "stopped" and scorer.STOPPED in app_scorer(client).lines
    client.post("/score", data={"csrf_token": csrf(client)})  # the earlier Stop does not stop this run
    assert app_scorer(client).running and not app_scorer(client).stopping
    run.release.set()
    wait_until_idle(app_scorer(client))
    assert app_scorer(client).state == "done"


def test_switching_automatic_scoring_off_stops_only_an_automatic_run(client, conn, run, ready, settings_path,
                                                                    monkeypatch):  # fmt: skip
    monkeypatch.setattr(scorer, "load_config", lambda: ready)
    run.release.clear()
    client.post("/score", data={"csrf_token": csrf(client)})  # started by you: it keeps going
    client.post("/settings", data=scoring_form() | {"csrf_token": csrf(client)})  # automatic: off
    assert app_scorer(client).running and not app_scorer(client).stopping
    run.release.set()
    wait_until_idle(app_scorer(client))
    run.release.clear()
    app_scorer(client).tick(conn)  # started by automatic scoring
    client.post("/settings", data=scoring_form() | {"csrf_token": csrf(client)})
    wait_until_idle(app_scorer(client))
    assert app_scorer(client).state == "stopped"


def test_the_collect_page_says_why_nothing_can_be_scored(client, data_dir, monkeypatch):
    monkeypatch.setattr(main, "load_config", lambda: settings(data_dir))
    assert "Scoring can't start: Resume not found: save your LaTeX resume as" in client.get("/collect").text
    (data_dir / RESUME_FILE).write_bytes(FAKE_RESUME)
    assert "Scoring can" not in client.get("/collect").text


@pytest.mark.parametrize(
    "path",
    ["/score", "/score/these", "/score/stop", "/jobs/{job}/score", "/jobs/{job}/prepare", "/jobs/{job}/answer",
     "/answer-key/score"],
)  # fmt: skip
def test_every_scoring_button_needs_the_anti_forgery_token(client, conn, run, path):
    with conn:
        job = add_job(conn, "ML Intern")
    data = {"value": "1", "label": "yes", "model": "m", "back": "/"}  # everything but the token
    assert client.post(path.format(job=job), data=data).status_code == 403
    assert run.calls == [] and not app_scorer(client).running
    assert conn.execute("SELECT prepare_anyway, (SELECT count(*) FROM answer_key) FROM jobs").fetchone() == (0, 0)
