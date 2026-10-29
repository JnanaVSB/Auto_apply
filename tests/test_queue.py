"""The scoring queue with a stand-in meaning model and a stand-in AI, made-up jobs and a fixed date."""

from datetime import UTC, datetime, timedelta

import pytest
from helpers import add_job
from test_stage_a import RESUME, FakeMeaning

from app import llm
from app.config import Config, ScoringSettings
from app.resume import Resume
from app.scoring import queue, stage_a

TODAY = "2026-10-28"
CONFIG = Config(data_dir="data", scoring=ScoringSettings(minutes_per_day=1))
SETUP = "setup-fp"
MY_RESUME = Resume(RESUME, "resume-fp")


@pytest.fixture(autouse=True)
def fixed_day(monkeypatch):
    monkeypatch.setattr(queue, "today", lambda: TODAY)
    monkeypatch.setattr(queue.time, "sleep", lambda seconds: None)


class FakeAI:
    """Robot jobs score 90, others 30; records the titles of the jobs asked about."""

    def __init__(self):
        self.titles = []

    def __call__(self, role, system, user, shape, *, retry):
        self.titles.append(user.split("<job>\n")[1].split("\n")[0])
        return '{"score": 90, "reasons": ["Robots."]}' if "Robot" in user else '{"score": 30, "reasons": ["No."]}'


def checker():
    return stage_a.Checker(["RL"], ["PyTorch"], RESUME, FakeMeaning())


def posted(days_ago):
    return (datetime.fromisoformat(TODAY) - timedelta(days=days_ago)).date().isoformat()


def run(conn, ai, **options):
    lines = []
    tally = queue.run(conn, CONFIG, checker(), SETUP, MY_RESUME, ask=ai, out=lines.append, **options)
    return tally, lines


def test_newest_first_then_strongest_match_and_nothing_older_than_90_days(conn):
    add_job(conn, "RL Intern A", description="Taxes.", date_posted=posted(20))  # 8-30 days, meaning 0
    add_job(conn, "RL Robot Engineer", description="Robots.", date_posted=posted(20))  # 8-30 days, meaning 1
    add_job(conn, "RL Analyst", description="Taxes.", date_posted=posted(3))  # this week
    add_job(conn, "RL Scientist", description="Taxes.", date_posted=posted(60))  # 31-90 days
    add_job(conn, "RL Lead", description="Taxes.", date_posted=posted(120))  # older: only with "Score these"
    add_job(conn, "Accountant", description="Taxes.", date_posted=posted(1))  # fails the quick check
    ai = FakeAI()
    tally, lines = run(conn, ai)
    assert ai.titles == ["RL Analyst", "RL Robot Engineer", "RL Intern A", "RL Scientist"]
    assert (tally.scored, tally.ready, tally.low) == (4, 1, 3)
    assert lines[-1].startswith("Scored 4 jobs: 1 ready (70 or more), 3 low score, 0 failed (check by hand)")


def test_only_out_of_date_scores_are_done_again(conn):
    job = add_job(conn, "RL Intern", description="Taxes.", date_posted=posted(1))
    run(conn, FakeAI())
    ai = FakeAI()
    run(conn, ai)
    assert ai.titles == []  # up to date
    with conn:
        conn.execute("UPDATE jobs SET description = 'Robots now.' WHERE id = ?", (job,))
    ai = FakeAI()
    run(conn, ai)
    assert ai.titles == ["RL Intern"]  # its ad changed
    ai = FakeAI()
    queue.run(conn, CONFIG, checker(), SETUP, Resume(RESUME, "new-resume-fp"), ask=ai, out=lambda line: None)
    assert ai.titles == ["RL Intern"]  # scored with an older resume


def test_a_failed_score_is_not_retried_every_day(conn):
    add_job(conn, "RL Intern", description="Taxes.", date_posted=posted(1))
    tally, _ = run(conn, lambda *args, **kwargs: "not JSON")
    assert tally.failed == 1
    ai = FakeAI()
    run(conn, ai)
    assert ai.titles == []


def test_the_day_stops_when_its_scoring_time_is_used_up(conn, monkeypatch):
    for number in range(3):
        add_job(conn, f"RL Intern {number}", description="Taxes.", date_posted=posted(1))
    with conn:
        conn.execute("INSERT INTO scoring_days VALUES (?, 59)", (TODAY,))  # 1 second left of 1 minute
    ticks = iter(range(0, 1000, 5))  # every job takes 5 seconds
    monkeypatch.setattr(queue.stage_b.time, "monotonic", lambda: next(ticks))
    tally, lines = run(conn, FakeAI())
    assert tally.scored == 1
    assert "Today's scoring time (0:01:00) is used up; scoring continues tomorrow." in lines
    assert conn.execute("SELECT seconds FROM scoring_days WHERE day = ?", (TODAY,)).fetchone()[0] == 64


def test_score_these_takes_any_age_and_does_not_use_the_days_time(conn):
    old = add_job(conn, "RL Lead", description="Taxes.", date_posted=posted(400))
    with conn:
        conn.execute("INSERT INTO scoring_days VALUES (?, 600)", (TODAY,))  # the day's time is used up
    ai = FakeAI()
    tally, _ = run(conn, ai, job_ids=[old])
    assert ai.titles == ["RL Lead"] and tally.scored == 1
    assert conn.execute("SELECT seconds FROM scoring_days").fetchone()[0] == 600


def test_scoring_waits_while_a_collection_runs(conn, monkeypatch):
    add_job(conn, "RL Intern", description="Taxes.", date_posted=posted(1))
    now = datetime.now(UTC)
    with conn:
        conn.execute("INSERT INTO collect_runs (started_at) VALUES (?)", (now.isoformat(timespec="seconds"),))
    checks = []

    def finish_collection(seconds):
        checks.append(seconds)
        with conn:
            conn.execute("UPDATE collect_runs SET finished_at = 'now'")

    monkeypatch.setattr(queue.time, "sleep", finish_collection)
    tally, lines = run(conn, FakeAI())
    assert lines[1:3] == [  # after the quick check's first line
        "Paused: a collection is running; scoring continues when it finishes.",
        "The collection has finished; scoring continues.",
    ]
    assert checks == [queue.PAUSE_CHECK_SECONDS] and tally.scored == 1


def test_a_collection_record_left_by_a_crash_does_not_block_scoring(conn):
    add_job(conn, "RL Intern", description="Taxes.", date_posted=posted(1))
    long_ago = (datetime.now(UTC) - timedelta(hours=7)).isoformat(timespec="seconds")
    with conn:
        conn.execute("INSERT INTO collect_runs (started_at) VALUES (?)", (long_ago,))
    tally, lines = run(conn, FakeAI())
    assert tally.scored == 1 and not any("Paused" in line for line in lines)


def test_a_problem_that_stops_scoring_marks_nothing_failed(conn):
    add_job(conn, "RL Intern", description="Taxes.", date_posted=posted(1))

    def not_running(*args, **kwargs):
        raise llm.StopScoring("Ollama isn't running: start it with `ollama serve`.")

    with pytest.raises(llm.StopScoring):
        run(conn, not_running)
    assert conn.execute("SELECT count(*) FROM ai_scores").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM stage_a").fetchone()[0] == 1  # the quick check is kept


def test_stopping_keeps_every_finished_job(conn):
    for number in range(3):
        add_job(conn, f"RL Intern {number}", description="Taxes.", date_posted=posted(1))
    calls = []

    def stop():
        calls.append(1)
        if len(calls) > 3:  # the quick check's one batch, then two jobs
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run(conn, FakeAI(), stop=stop)
    assert conn.execute("SELECT count(*) FROM ai_scores").fetchone()[0] == 2
    ai = FakeAI()
    run(conn, ai)
    assert len(ai.titles) == 1  # the next run continues with the rest


def test_three_jobs_failing_the_same_way_stop_the_run_and_stay_waiting(conn):
    for number in range(4):
        add_job(conn, f"RL Intern {number}", description="Taxes.", date_posted=posted(1))
    with pytest.raises(llm.StopScoring, match="3 jobs in a row failed the same way") as stopped:
        run(conn, lambda *args, **kwargs: "not JSON")
    assert "not in the agreed shape" in str(stopped.value)
    assert conn.execute("SELECT count(*) FROM ai_scores").fetchone()[0] == 0  # nothing marked failed
    ai = FakeAI()
    run(conn, ai)
    assert len(ai.titles) == 4  # all four are scored next time


def test_failures_are_saved_once_the_next_job_goes_differently(conn):
    add_job(conn, "RL Intern 1", description="Taxes.", date_posted=posted(1))
    add_job(conn, "RL Intern 2", description="Taxes.", date_posted=posted(1))
    add_job(conn, "RL Robot Engineer", description="Robots.", date_posted=posted(20))  # scored last: older

    def taxes_fail(role, system, user, shape, *, retry):
        return '{"score": 90, "reasons": []}' if "Robot" in user else "not JSON"

    tally, _ = run(conn, taxes_fail)
    assert (tally.scored, tally.failed, tally.ready) == (3, 2, 1)
    saved = conn.execute("SELECT count(failed), count(score) FROM ai_scores").fetchone()
    assert saved == (2, 1)


def test_scoring_chosen_jobs_quick_checks_only_those(conn):
    chosen = add_job(conn, "RL Intern", description="Taxes.", date_posted=posted(1))
    for number in range(3):
        add_job(conn, f"RL Analyst {number}", description="Taxes.", date_posted=posted(1))
    ai = FakeAI()
    run(conn, ai, job_ids=[chosen])
    assert ai.titles == ["RL Intern"]
    assert conn.execute("SELECT job_id FROM stage_a").fetchall() == [(chosen,)]  # not every open job


def test_a_failed_new_try_is_not_repeated_until_the_resume_or_ad_changes(conn):
    job = add_job(conn, "RL Intern", description="Taxes.", date_posted=posted(1))
    run(conn, FakeAI())
    new_resume = Resume(RESUME, "new-resume-fp")
    tries = []

    def bad(*args, **kwargs):
        tries.append(1)
        return "not JSON"

    for _ in range(2):  # the failed new try is made once, not again on the next run
        queue.run(conn, CONFIG, checker(), SETUP, new_resume, ask=bad, out=lambda line: None)
    assert len(tries) == 2  # one job, two tries, one run
    assert conn.execute("SELECT score FROM ai_scores").fetchone()[0] == 30  # the older score stays
    with conn:
        conn.execute("UPDATE jobs SET description = 'Robots now.' WHERE id = ?", (job,))
    ai = FakeAI()
    queue.run(conn, CONFIG, checker(), SETUP, new_resume, ask=ai, out=lambda line: None)
    assert ai.titles == ["RL Intern"]  # its ad changed: tried again
