"""Automatic preparation, its cost estimates and the terminal command, with made-up jobs (nothing reaches an AI)."""

import json
from datetime import UTC, datetime

import pytest
from helpers import add_job, one
from test_flow import GOOD, made_up_job
from test_runs import LOCAL
from test_web_ready import CHOSEN, scored

from app import prepare
from app.config import AIRole, AISettings, Config, CouncilSettings, SpendingSettings
from app.council import automatic, estimate, flow, runner
from app.keychain import SERVICE
from app.resume import from_latex

OPUS = AIRole(service="anthropic", model="claude-opus-5-5", key_name="anthropic", thinking="high")
SOL = AIRole(service="openai-compatible", model="gpt-6.1-sol", address="https://api.openai.com/v1", key_name="openai",
             thinking="high")  # fmt: skip
MEMBERS = ("truth", "hiring", "editor", "ats", "domain")
NIGHT = datetime(2026, 11, 2, 23, 30).astimezone()  # inside the default council hours, on this computer's clock
DAY = datetime(2026, 11, 2, 14, 0).astimezone()


@pytest.fixture(autouse=True)
def built(monkeypatch):
    monkeypatch.setattr(flow.pdf, "build_kept", lambda data_dir, target, source: None)  # never Tectonic here


def settings(data_dir, writer=LOCAL, reviewer=LOCAL, automatic_=True, daily_jobs=20, daily=15, **council):
    return Config(
        data_dir=data_dir, ai=AISettings(writer=writer, **dict.fromkeys(MEMBERS, reviewer)),
        council=CouncilSettings(automatic=automatic_, daily_jobs=daily_jobs, **council),
        spending=SpendingSettings(daily=daily),
    )  # fmt: skip


@pytest.fixture
def ready_jobs(conn, data_dir):
    """Three ready jobs (posted on different days) beside the made-up Profile; returns their ids, newest first."""
    first = made_up_job(conn, data_dir)
    resume = from_latex((data_dir / "resume.tex").read_text(encoding="utf-8")).fingerprint
    with conn:
        jobs = [add_job(conn, f"Job {n}", date_posted=f"2026-10-0{n}") for n in (1, 2, 3)]
        for job in jobs:
            scored(conn, job, score=90, resume=resume)
        conn.execute("UPDATE jobs SET status = 'closed' WHERE id = ?", (first,))  # not ready: out of the way
    return jobs[::-1]


def test_the_estimate_counts_every_call_at_the_round_limit_and_the_laptop_is_free(conn):
    assert estimate.version(conn, LOCAL, dict.fromkeys(MEMBERS, LOCAL), 3, 1, half=True) == 0
    one_round = estimate.version(conn, OPUS, {}, 1, 0, half=True)
    assert one_round == pytest.approx((6500 * 2 + (3000 + 3000) * 10) / 1e6)  # the first draft, thinking at high
    two = estimate.version(conn, OPUS, {"truth": SOL}, 2, 1, half=True)
    review = lambda reading: (reading * 1 + (500 + 3000) * 5) / 1e6  # noqa: E731
    revision = (11500 * 2 + 6000 * 10) / 1e6
    assert two == pytest.approx(one_round + revision + review(9300) + review(10800) + review(11300) + review(12800))
    assert estimate.version(conn, OPUS, {}, 1, 0, half=False) == pytest.approx(2 * one_round)  # normal price
    own = AIRole(service="openai-compatible", model="m", address="https://models.example.org/v1")
    assert estimate.version(conn, own, {}, 1, 0, half=True) is None  # no price: can't be estimated


def test_after_twenty_finished_versions_your_own_averages_are_used(conn, data_dir):
    job = made_up_job(conn, data_dir)
    with conn:
        for number in range(1, 21):
            prep = conn.execute("INSERT INTO preparations (job_id, kind, version, sources, writer, status, draft, "
                                "created_on) VALUES (?, 'resume', ?, '[]', '{}', 'done', '{}', 'now')",
                                (job, number)).lastrowid  # fmt: skip
            conn.execute("INSERT INTO steps (preparation_id, service, model, status, input_tokens, output_tokens, "
                         "cost, day, sent_on) VALUES (?, 'anthropic', 'claude-opus-5-5', 'done', 1000, 100, 0, 'd', "
                         "'now')", (prep,))  # fmt: skip
    assert estimate.version(conn, OPUS, {}, 1, 0, half=True) == pytest.approx((1000 * 2 + 100 * 10) / 1e6)


def test_automatic_preparation_starts_ready_jobs_newest_first_within_the_daily_cap(conn, data_dir, ready_jobs):
    config = settings(data_dir, daily_jobs=2)
    waits = automatic.start_due(conn, config, NIGHT)
    started = [row[0] for row in conn.execute("SELECT job_id FROM preparations WHERE automatic = 1 ORDER BY id")]
    assert started == ready_jobs[:2] and "today's job cap of 2 is reached" in waits
    assert automatic.start_due(conn, config, NIGHT) == waits and len(started) == 2  # no job started twice


def test_your_presses_count_toward_the_cap_but_jobs_prepared_before_are_left_alone(conn, data_dir, ready_jobs):
    config = settings(data_dir, daily_jobs=2)
    flow.start(conn, config, ready_jobs[0])  # your press today
    automatic.start_due(conn, config, NIGHT)
    started = [row[0] for row in conn.execute("SELECT job_id FROM preparations WHERE automatic = 1")]
    assert started == [ready_jobs[1]]  # the pressed job isn't started again; the cap counts your press


def test_automatic_preparation_waits_outside_the_council_hours_or_when_off(conn, data_dir, ready_jobs):
    assert automatic.start_due(conn, settings(data_dir), DAY) == (
        "Automatic preparation waits for the council hours (22:00 to 08:00)."
    )
    assert automatic.start_due(conn, settings(data_dir, automatic_=False), NIGHT) is None
    assert one(conn, "SELECT count(*) FROM preparations")[0] == 0
    automatic.start_due(conn, settings(data_dir, any_time=True), DAY)
    assert one(conn, "SELECT count(*) FROM preparations")[0] == 3


@pytest.mark.parametrize(("time", "inside"), [("22:00", True), ("03:15", True), ("07:59", True), ("08:00", False),
                                               ("21:59", False)])  # fmt: skip
def test_council_hours_may_run_past_midnight(time, inside):
    assert automatic._in_hours(time, "22:00", "08:00") is inside
    assert automatic._in_hours(time, "08:00", "22:00") is not inside


def test_a_paid_member_without_a_key_makes_it_wait(conn, data_dir, ready_jobs, fake_keychain):
    config = settings(data_dir, writer=OPUS)
    assert "no key is saved under 'anthropic'" in automatic.start_due(conn, config, NIGHT)
    fake_keychain.store[(SERVICE, "anthropic")] = "fake-key"
    automatic.start_due(conn, config, NIGHT)
    assert one(conn, "SELECT count(*) FROM preparations")[0] == 3


def test_a_job_starts_only_if_its_estimate_fits_what_is_left_after_the_jobs_running(conn, data_dir, ready_jobs,
                                                                                     fake_keychain):  # fmt: skip
    fake_keychain.store[(SERVICE, "anthropic")] = "fake-key"
    config = settings(data_dir, writer=OPUS, daily=1, rounds=5)  # about $0.41 per job (an Opus Writer, 5 rounds)
    waits = automatic.start_due(conn, config, NIGHT)
    assert one(conn, "SELECT count(*) FROM preparations")[0] == 2  # the third would pass $1 with the two running
    assert "estimated cost doesn't fit" in waits
    with conn:
        conn.execute("DELETE FROM preparations WHERE id = 2")  # one of them gone: room for one more
    automatic.start_due(conn, config, NIGHT)
    assert one(conn, "SELECT count(*) FROM preparations")[0] == 2  # the one still running counts its estimate


def test_a_job_stopped_because_your_entries_changed_starts_again(conn, data_dir, ready_jobs):
    config = settings(data_dir, daily_jobs=5)
    stopped = flow.start(conn, config, ready_jobs[0])
    with conn:
        conn.execute("UPDATE preparations SET status = 'stopped', sources = '[]' WHERE id = ?", (stopped,))
    automatic.start_due(conn, config, NIGHT)
    assert [row[0] for row in conn.execute("SELECT job_id FROM preparations WHERE automatic = 1")] == ready_jobs


def test_resume_only_is_the_writer_alone_and_words_while_writing_wait_for_its_next_round(conn, data_dir, monkeypatch):
    job = made_up_job(conn, data_dir)
    config = settings(data_dir)
    calls = []
    monkeypatch.setattr(runner.llm, "call", lambda role, system, user, *a, **k: calls.append(user) or
                        runner.llm.Reply(GOOD, runner.llm.Usage()))  # fmt: skip
    monkeypatch.setattr(runner, "_scoring", lambda config: False)
    alone = flow.start(conn, config, job, writer_only=True)
    assert json.loads(one(conn, "SELECT council FROM preparations")[0]) == {"members": {}, "names": {}}
    flow.ask(conn, alone, "Lead with the drone.")
    runner.run(conn, config, runner.State(), datetime.now(UTC))
    assert one(conn, "SELECT status, agreed FROM preparations") == ("done", None) and len(calls) == 1
    assert [item["words"] for item in json.loads(one(conn, "SELECT asked FROM preparations")[0])] == [
        "Lead with the drone."
    ]


def test_the_ready_order_is_newest_first(conn, data_dir, ready_jobs):
    from app.scoring import ready

    resume = from_latex((data_dir / "resume.tex").read_text(encoding="utf-8")).fingerprint
    assert ready.newest(conn, CHOSEN.model, 70, resume, 10) == ready_jobs


def test_the_terminal_command_runs_a_pass_a_minute_until_ctrl_c(data_dir, monkeypatch, capsys):
    monkeypatch.setattr(prepare, "load_config", lambda: settings(data_dir))
    monkeypatch.setattr(prepare, "one_pass", lambda config, state: "23:30 0 answers saved")
    monkeypatch.setattr(prepare.time, "sleep", lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert prepare.main([]) == 0
    assert capsys.readouterr().out == "23:30 0 answers saved\nStopped. Everything finished so far is saved; run the " \
                                      "command again to carry on.\n"  # fmt: skip


def test_a_terminal_pass_says_what_it_did_in_one_line(conn, data_dir, monkeypatch):
    config = settings(data_dir, automatic_=False)
    monkeypatch.setattr(prepare, "load_config", lambda: config)
    line = prepare.one_pass(config, runner.State())
    assert line.endswith("0 answers saved; 0 versions working; 0 calls at Anthropic; 0 calls waiting")
