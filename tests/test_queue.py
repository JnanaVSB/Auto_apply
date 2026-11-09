"""The scoring queue with a stand-in meaning model and a stand-in AI, made-up jobs and a fixed date."""

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from helpers import add_job, one
from test_spending import calls
from test_stage_a import RESUME, FakeMeaning
from test_tiles import RESUME_CLS

from app import llm, questionnaire
from app.config import AIRole, AISettings, Config, ScoringSettings, SpendingSettings
from app.council import spending
from app.documents import privacy, tile_sets
from app.documents.tiles import Tile
from app.resume import Resume
from app.scoring import queue, stage_a, stage_b

TODAY = "2026-10-28"
CONFIG = Config(data_dir="data", scoring=ScoringSettings(minutes_per_day=1))
SETUP = "setup-fp"
MY_RESUME = Resume(RESUME, "resume-fp")
OWN = AIRole(service="openai-compatible", model="my-model", address="https://scoring.example.net/v1")
CONTACT = {"full_name": "Alex Example", "email": "alex@example.com",
           "linkedin": "https://www.linkedin.com/in/alex-example-fake"}  # fmt: skip
PAPER = Tile("pub-0001", "publication", "Publications", {"name": "Fake Paper. 2025.",
             "link": "https://example.org/fake-paper"})  # fmt: skip
OPUS = AIRole(service="anthropic", model="claude-opus-5-5", key_name="anthropic")
USED = llm.Usage(2000, 0, 500)  # Opus: 2000 x $4 + 500 x $20 per 1M = $0.018
MONEY = "SELECT calls, input_tokens, cached_tokens, output_tokens, cost, reserved, uncounted FROM scoring_days"


@pytest.fixture(autouse=True)
def fixed_day(monkeypatch):
    monkeypatch.setattr(queue, "today", lambda: TODAY)
    monkeypatch.setattr(spending, "today", lambda: TODAY)
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


class Sent:
    """A stand-in for your own address: records each text sent, and answers with your name's placeholder."""

    def __init__(self):
        self.texts = []

    def __call__(self, role, system, user, shape, *, retry):
        self.texts.append(user)
        return '{"score": 90, "reasons": ["[my name] fits."]}'


def replies(ai, usage=llm.NOTHING):
    """A stand-in AI that gives only an answer's text, sent as llm.call sends it: with the tokens counted for it."""
    return lambda *found, **options: llm.Reply(ai(*found, **options), usage)


def run(conn, ai, config=CONFIG, resume=MY_RESUME, usage=llm.NOTHING, **options):
    lines = []
    tally = queue.run(conn, config, checker(), SETUP, resume, call=replies(ai, usage), out=lines.append, **options)
    return tally, lines


def own_address(data_dir, ticked=OWN.address):
    (data_dir / "resume.tex").write_text(RESUME_CLS, encoding="utf-8")
    return Config(data_dir=data_dir, scoring=ScoringSettings(cloud_ok=ticked), ai=AISettings(evaluator=OWN))


def paid(data_dir, role=OPUS, daily=15, monthly=150):
    """Scoring through a service you ticked, within these spending limits."""
    return own_address(data_dir, role.address).model_copy(
        update={"ai": AISettings(evaluator=role), "spending": SpendingSettings(daily=daily, monthly=monthly)}
    )


def you_and_a_job(conn):
    questionnaire.save(conn, questionnaire.check({"contact": CONTACT}))
    return add_job(conn, "RL Intern", description="Taxes.", date_posted=posted(1))


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
    queue.run(conn, CONFIG, checker(), SETUP, Resume(RESUME, "new-resume-fp"), call=replies(ai), out=lambda line: None)
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
        conn.execute("INSERT INTO scoring_days (day, seconds) VALUES (?, 59)", (TODAY,))  # 1 second left of 1 minute
    ticks = iter(range(0, 1000, 5))  # every job takes 5 seconds
    monkeypatch.setattr(queue.stage_b.time, "monotonic", lambda: next(ticks))
    tally, lines = run(conn, FakeAI())
    assert tally.scored == 1
    assert "Today's scoring time (0:01:00) is used up; scoring continues tomorrow." in lines
    assert conn.execute("SELECT seconds FROM scoring_days WHERE day = ?", (TODAY,)).fetchone()[0] == 64


def test_score_these_takes_any_age_and_does_not_use_the_days_time(conn):
    old = add_job(conn, "RL Lead", description="Taxes.", date_posted=posted(400))
    with conn:
        conn.execute("INSERT INTO scoring_days (day, seconds) VALUES (?, 600)", (TODAY,))  # the day's time is used up
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


def test_three_jobs_failing_the_same_way_stop_the_run_and_a_repeat_moves_on(conn, monkeypatch):
    jobs = [add_job(conn, f"RL Intern {number}", description="Taxes.", date_posted=posted(1)) for number in range(5)]
    ticks = iter(range(0, 1000, 5))  # every job takes 5 seconds
    monkeypatch.setattr(queue.stage_b.time, "monotonic", lambda: next(ticks))
    for repeat in (1, 2):
        with pytest.raises(llm.StopScoring) as stopped:
            run(conn, lambda *args, **kwargs: "not JSON")
        assert str(stopped.value).startswith(
            "3 jobs in a row failed the same way: AI score failed twice (the answer was not in the agreed shape; then "
            "the answer was not in the agreed shape). The cause is most likely Ollama"
        )
        assert "check this job by hand" not in str(stopped.value)
        failed = conn.execute("SELECT job_id FROM ai_scores WHERE failed IS NOT NULL ORDER BY job_id").fetchall()
        assert failed == [(job,) for job in jobs[:repeat]]  # only the first of the 3 is saved, as failed
        assert queue.used_today(conn) == 15 * repeat  # all 3 jobs' time counts
    ai = FakeAI()
    run(conn, ai)
    assert ai.titles == ["RL Intern 2", "RL Intern 3", "RL Intern 4"]  # the other two of the 3 waited


def test_held_failures_are_saved_when_the_days_time_is_used_up(conn, monkeypatch):
    for number in range(3):
        add_job(conn, f"RL Intern {number}", description="Taxes.", date_posted=posted(1))
    with conn:
        conn.execute("INSERT INTO scoring_days (day, seconds) VALUES (?, 57)", (TODAY,))  # 3 seconds left of 1 minute
    ticks = iter(range(0, 1000, 5))  # every job takes 5 seconds
    monkeypatch.setattr(queue.stage_b.time, "monotonic", lambda: next(ticks))
    tally, lines = run(conn, lambda *args, **kwargs: "not JSON")
    assert tally.failed == 1 and "Today's scoring time (0:01:00) is used up; scoring continues tomorrow." in lines
    assert conn.execute("SELECT count(failed) FROM ai_scores").fetchone()[0] == 1  # the held failure is saved
    assert queue.used_today(conn) == 62


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
        queue.run(conn, CONFIG, checker(), SETUP, new_resume, call=replies(bad), out=lambda line: None)
    assert len(tries) == 2  # one job, two tries, one run
    assert conn.execute("SELECT score FROM ai_scores").fetchone()[0] == 30  # the older score stays
    with conn:
        conn.execute("UPDATE jobs SET description = 'Robots now.' WHERE id = ?", (job,))
    ai = FakeAI()
    queue.run(conn, CONFIG, checker(), SETUP, new_resume, call=replies(ai), out=lambda line: None)
    assert ai.titles == ["RL Intern"]  # its ad changed: tried again


def test_your_own_address_gets_your_resume_with_your_questionnaire_header_and_entry_details_hidden(conn, tmp_path):
    add_job(conn, "RL Intern", description="Taxes.", date_posted=posted(1))
    questionnaire.save(conn, questionnaire.check({"contact": CONTACT}))
    tile_sets.confirm(conn, [PAPER], "fp")
    text = "Alex Example, alex@example.com, (555) 010-0199, example.com/alex. Paper: https://example.org/fake-paper"
    ai = Sent()
    run(conn, ai, own_address(tmp_path), Resume(text, "resume-fp"))
    resume = ai.texts[0].split("<job>")[0]
    assert not re.search("alex|555|example", resume, re.I)  # the header's phone and link, the entry's link
    assert stage_b.OTHER_LINK not in resume  # each link is one of yours, so each got its own placeholder
    assert json.loads(conn.execute("SELECT reasons FROM ai_scores").fetchone()[0]) == ["Alex Example fits."]


def test_your_own_address_needs_your_full_name_first_and_is_sent_nothing_without_it(conn, tmp_path):
    add_job(conn, "RL Intern", description="Taxes.", date_posted=posted(1))
    ai = Sent()
    with pytest.raises(llm.StopScoring, match=re.escape(privacy.NO_NAME)):
        run(conn, ai, own_address(tmp_path))
    assert ai.texts == []


def test_an_address_you_never_ticked_stops_the_run_before_anything_is_done(conn, tmp_path):
    add_job(conn, "RL Intern", description="Taxes.", date_posted=posted(1))
    questionnaire.save(conn, questionnaire.check({"contact": CONTACT}))
    ai = Sent()
    with pytest.raises(llm.StopScoring, match="isn't ticked for this address"):
        run(conn, ai, own_address(tmp_path, ticked=""))
    assert ai.texts == [] and conn.execute("SELECT count(*) FROM stage_a").fetchone()[0] == 0


def test_a_paid_request_within_your_limits_is_sent_through_the_filter_and_its_cost_counted(conn, tmp_path):
    you_and_a_job(conn)
    ai = Sent()
    run(conn, ai, paid(tmp_path), Resume("Alex Example trained robots in PyTorch.", "resume-fp"), usage=USED)
    assert "Alex" not in ai.texts[0] and "[my name]" in ai.texts[0]
    assert one(conn, MONEY) == (1, 2000, 0, 500, pytest.approx(0.018), 0, 0)  # what was set aside became its cost
    assert spending.spent(conn, TODAY) == pytest.approx((0.018, 0.018))  # the council's limits see it


@pytest.mark.parametrize(
    ("day", "council", "line"),
    [
        (TODAY, 14.99, "today's spending limit of $15.00 would be passed ($14.99 spent today"),
        ("2026-10-02", 149.99, "this month's spending limit of $150.00 would be passed ($149.99 spent this month"),
    ],
)
def test_a_paid_request_past_your_limits_is_refused_and_scoring_waits_until_tomorrow(conn, tmp_path, day, council,
                                                                                     line):  # fmt: skip
    calls(conn, ("done", council, 0, day), job=you_and_a_job(conn))  # the council's spend counts against them too
    ai = Sent()
    with pytest.raises(llm.StopScoring, match=re.escape(line)):
        run(conn, ai, paid(tmp_path))
    assert ai.texts == [] and one(conn, MONEY) == (0, 0, 0, 0, 0, 0, 0)  # nothing sent, nothing set aside
    assert queue.used_today(conn) == 120 * 60  # automatic scoring waits until midnight instead of trying again


def test_your_own_address_without_a_price_is_sent_but_not_counted(conn, tmp_path):
    you_and_a_job(conn)
    run(conn, Sent(), paid(tmp_path, OWN, daily=1), usage=USED)
    assert one(conn, MONEY) == (1, 2000, 0, 500, 0, 0, 1)  # its tokens kept, its dollars "at least"
    assert spending.history(conn, "day")[0].uncounted


@pytest.mark.parametrize(
    "error", [llm.LLMError("the answer was cut off", USED), llm.TimedOut("no answer within 120 seconds")]
)
def test_a_failed_paid_request_still_counts_what_it_may_have_cost(conn, tmp_path, error):
    you_and_a_job(conn)

    def failing(*found, **options):
        raise error

    run(conn, failing, paid(tmp_path))
    sent, *tokens, cost, reserved, _ = one(conn, MONEY)
    assert (sent, reserved) == (2, 0)  # the try and its retry, each ended
    if error.usage != llm.NOTHING:  # charged for what it wrote before it failed (an empty Usage is still truthy)
        assert (tokens, cost) == ([4000, 0, 1000], pytest.approx(0.036))
    else:  # no answer, so perhaps charged up to what was set aside: far more than what it read
        assert tokens == [0, 0, 0] and cost > 0.036


def test_what_a_stopped_run_set_aside_stays_counted_as_spent(conn):
    with conn:
        conn.execute("INSERT INTO scoring_days (day, calls, reserved) VALUES ('2026-10-27', 1, 0.5)")
    _, lines = run(conn, FakeAI())
    assert one(conn, "SELECT cost, reserved FROM scoring_days WHERE day = '2026-10-27'") == (0.5, 0)
    assert "The last scoring run stopped while waiting for an answer: up to $0.50 is counted as spent." in lines
    _, lines = run(conn, FakeAI())
    assert not any("stopped while waiting" in line for line in lines)  # said once


def test_scoring_on_this_computer_counts_no_money(conn):
    add_job(conn, "RL Robot Engineer", description="Robots.", date_posted=posted(1))
    tally, _ = run(conn, FakeAI(), usage=USED)
    assert tally.scored == 1 and one(conn, MONEY) == (0, 0, 0, 0, 0, 0, 0) and spending.history(conn, "day") == []
