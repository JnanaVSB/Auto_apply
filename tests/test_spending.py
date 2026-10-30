"""Money for paid AI calls: added up from the saved steps, and kept within the daily and monthly limits."""

from contextlib import closing

import pytest
from helpers import add_job

from app import db
from app.config import AIRole, SpendingSettings
from app.council import spending
from app.llm import Usage

LOCAL = AIRole(model="gemma4:26b-a4b-it-q8_0")
OPUS = AIRole(service="anthropic", model="claude-opus-5-5", key_name="anthropic")
OWN = AIRole(service="openai-compatible", model="my-model", address="https://models.example.org/v1")
LIMITS = SpendingSettings(daily=15, monthly=150)
PREPARATION = """INSERT INTO preparations (job_id, kind, version, sources, writer, status, created_on)
    VALUES (?, 'resume', ?, '[]', '{}', 'stopped', 'now')"""
STEP = """INSERT INTO steps (preparation_id, service, model, status, cost, reserved, day, sent_on, input_tokens,
    cached_tokens, output_tokens) VALUES (?, 'anthropic', 'claude-opus-5-5', ?, ?, ?, ?, 'now', ?, ?, ?)"""


@pytest.fixture
def conn(data_dir):
    with closing(db.connect(data_dir)) as conn:
        yield conn


def calls(conn, *steps, job=None):
    """Saved steps (status, cost, reserved, day, and optionally tokens in, cached, out), each in a preparation of its
    own for one job."""
    with conn:
        job = job or add_job(conn, "ML Intern")
        first = conn.execute("SELECT count(*) FROM preparations WHERE job_id = ?", (job,)).fetchone()[0] + 1
        for version, step in enumerate(steps, first):
            conn.execute(STEP, (conn.execute(PREPARATION, (job, version)).lastrowid, *(step + (0, 0, 0))[:7]))
    return job


def test_spending_adds_up_the_days_steps_and_what_is_set_aside(conn):
    calls(
        conn,
        ("done", 1.25, 0, "2026-10-30"),
        ("sent", 0, 0.40, "2026-10-30"),  # waiting: its largest possible cost counts
        ("done", None, 0, "2026-10-30"),  # not counted
        ("failed", 2.00, 0, "2026-10-02"),  # earlier this month
        ("done", 9.00, 0, "2026-09-30"),  # last month
    )
    assert spending.spent(conn, "2026-10-30") == pytest.approx((1.65, 3.65))
    assert spending.spent(conn, "2026-11-01") == (0, 0)


def test_the_largest_cost_is_counted_generously():
    texts = "x" * 2000  # 1,000 tokens at 2 bytes each; an answer of 8,192 tokens plus 8,000 for thinking
    assert spending.largest(OPUS, texts, 8192) == pytest.approx((1000 * 4 + 16192 * 20) / 1_000_000)
    assert spending.largest(OPUS, "é" * 1000, 8192) == spending.largest(OPUS, texts, 8192)  # é is 2 bytes
    assert spending.largest(LOCAL, texts, 8192) == 0
    assert spending.largest(OWN, texts) is None
    priced = OWN.model_copy(update={"price_input": 1, "price_output": 2})
    assert spending.largest(priced, texts) == pytest.approx((1000 * 1 + 4096 * 2) / 1_000_000)


def test_the_real_cost_comes_from_the_reported_tokens():
    assert spending.charged(OPUS, Usage(1000, 500, 2000)) == pytest.approx((1000 * 4 + 500 * 0.2 + 2000 * 20) / 1e6)
    assert spending.charged(LOCAL, Usage(1000, 0, 2000)) == 0
    assert spending.charged(OWN, Usage(1000, 0, 2000)) is None


@pytest.mark.parametrize(
    ("today", "earlier", "cost", "problem"),
    [
        (14.50, 0, 0.50, None),  # exactly at the limit is within it
        (14.50, 0, 0.51, "today's spending limit of $15.00 would be passed ($14.50 spent today; this call could "
                         "cost up to $0.51). It continues tomorrow"),
        (0, 149.80, 0.10, None),
        (0, 149.80, 0.30, "this month's spending limit of $150.00 would be passed ($149.80 spent this month; this "
                          "call could cost up to $0.30). It continues next month"),
    ],
)  # fmt: skip
def test_a_paid_call_must_fit_within_both_limits(conn, today, earlier, cost, problem):
    calls(conn, ("done", today, 0, "2026-10-30"), ("done", earlier, 0, "2026-10-01"))
    found = spending.refusal(conn, OPUS, cost, LIMITS, "2026-10-30")
    assert found == problem if problem is None else problem in found


def test_free_and_uncounted_calls_are_never_stopped_but_an_unlisted_model_is(conn):
    calls(conn, ("done", 500, 0, "2026-10-30"))
    assert spending.refusal(conn, LOCAL, 0, LIMITS, "2026-10-30") is None
    assert spending.refusal(conn, OWN, None, LIMITS, "2026-10-30") is None
    unlisted = OPUS.model_copy(update={"model": "claude-unknown-9"})
    assert "claude-unknown-9 is not on the app's price list" in spending.refusal(conn, unlisted, None, LIMITS, "x")


def test_a_jobs_cost_adds_up_its_steps_and_says_when_some_was_not_counted(conn):
    job = calls(conn, ("done", 0.30, 0, "2026-10-30"), ("sent", 0, 0.40, "2026-10-30"))
    other = calls(conn, ("done", 1, 0, "2026-10-30"))
    assert spending.of_job(conn, job) == (pytest.approx(0.70), False)
    calls(conn, ("done", None, 0, "2026-10-30"), job=other)
    assert spending.of_job(conn, other) == (1, True)
    assert spending.of_job(conn, 999) == (0, False)


def test_the_history_shows_every_day_week_month_and_year_newest_first(conn):
    calls(
        conn,
        ("done", 1.00, 0, "2026-10-30", 1000, 100, 500),  # a Friday
        ("done", None, 0, "2026-10-30", 10, 0, 5),  # not counted
        ("sent", 0, 0.40, "2026-10-26", 2000, 0, 0),  # Monday of the same week, still waiting
        ("done", 2.00, 0, "2026-10-25", 3000, 0, 700),  # Sunday of the week before
        ("done", 0.50, 0, "2025-12-31", 4000, 0, 900),
    )
    friday, monday, sunday = (
        (1, 1010, 100, 505, 1.00, True),
        (1, 2000, 0, 0, 0.40, False),
        (1, 3000, 0, 700, 2.00, False),
    )
    days = spending.history(conn, "day")
    assert [day.start for day in days] == ["2026-10-30", "2026-10-26", "2026-10-25", "2025-12-31"]
    assert tuple(days[0][1:]) == (2, *friday[1:]) and tuple(days[1][1:]) == monday
    weeks = spending.history(conn, "week")
    assert [week.start for week in weeks] == ["2026-10-26", "2026-10-19", "2025-12-29"]
    assert tuple(weeks[0][1:]) == pytest.approx((3, 3010, 100, 505, 1.40, True))
    assert tuple(weeks[1][1:]) == sunday
    months = spending.history(conn, "month")
    assert [(month.start, month.calls, month.uncounted) for month in months] == [
        ("2026-10-01", 4, True), ("2025-12-01", 1, False)
    ]  # fmt: skip
    years = spending.history(conn, "year")
    assert [(year.start, year.calls, year.output) for year in years] == [
        ("2026-01-01", 4, 1205),
        ("2025-01-01", 1, 900),
    ]
    assert years[0].dollars == pytest.approx(3.40)


def test_no_calls_means_no_history(conn):
    assert spending.history(conn, "week") == []
