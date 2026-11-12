"""The Summary page: what each part counts as of a fixed day, its windows on local dates, the page itself, and landing
there after sign-in."""

import re
from datetime import UTC, date, datetime, timedelta

import pytest
from helpers import add_job, log_in
from test_tracker import CHANGED, version
from test_web_scoring import CHOSEN, scored

from app.web import main, summary

TODAY = date(2026, 11, 12)
WEEK, MONTH, FIRST = date(2026, 11, 6), date(2026, 10, 14), date(2026, 8, 14)  # the first days of each window


def utc(day, *time):
    """This computer's local time on `day`, as the UTC text the app stores."""
    return datetime(day.year, day.month, day.day, *time).astimezone(UTC).isoformat(timespec="seconds")


def found(day):
    """A job's first_seen at the very start of a local day."""
    return {"first_seen": utc(day, 0, 0, 0)}


def gather(conn):
    return summary.gather(conn, CHOSEN, TODAY)


def written(conn, title="Written", **values):
    """A job the Ready to Apply page shows with a finished resume version: ready to review."""
    job = add_job(conn, title, **values)
    scored(conn, job, score=85)
    version(conn, job, on=utc(TODAY, 9, 0, 0))
    return job


def test_an_empty_database_counts_nothing_and_points_to_collect(conn):
    counts = gather(conn)
    assert {name: counts[name] for name in summary.NAMES} == dict.fromkeys(summary.NAMES, 0)
    assert counts["median"] is None and counts["to_review"] == [] and counts["moved"] == [] and not counts["gmail"]
    assert counts["next"] == ("Collect new jobs", "/collect")
    assert all(
        chart["collected"] == chart["sent"] == 0 and chart["per_100"] is None for chart in counts["charts"].values()
    )


def test_one_new_job_is_open_found_this_week_and_waiting(conn):
    with conn:
        add_job(conn, "Engineer", **found(TODAY))
    counts = gather(conn)
    assert (counts["open"], counts["open_week"], counts["collected"], counts["waiting"]) == (1, 1, 1, 1)
    assert counts["charts"][7]["columns"][-1]["collected"] == 1
    assert counts["next"] == ("Score 1 waiting job", "/collect#scoring")


def test_a_written_job_is_ready_to_review(conn):
    with conn:
        job = written(conn)
    counts = gather(conn)
    assert (counts["ready"], counts["ready_week"], counts["unwritten"]) == (1, 1, 0)
    assert counts["to_review"] == [(job, "Written", "Acme", 85)]
    assert counts["next"] == ("Review 1 ready application", "/ready")


@pytest.mark.parametrize(
    "exclude",
    [
        "UPDATE jobs SET not_interested = 1",
        "UPDATE jobs SET status = 'closed'",
        "INSERT INTO tracker (job_id, stage, applied_on, changed_on) SELECT id, 'applied', '2026-11-10', 'x' FROM jobs",
        "INSERT INTO submissions (job_id, submitted_on, saved_on, how, address, fields, files) "
        "SELECT id, '2026-11-10', 'x', 'by hand', 'x', '[]', '[]' FROM jobs",
        "UPDATE ai_scores SET resume = 'resume-before'",
        "UPDATE preparations SET status = 'stopped'",
    ],
    ids=["skipped", "closed", "past Ready", "submitted", "stale score", "no finished resume"],
)
def test_ready_to_review_leaves_out(conn, exclude):
    with conn:
        written(conn)
        conn.execute(exclude)
    counts = gather(conn)
    assert counts["ready"] == 0 and counts["to_review"] == []


def test_a_ready_job_without_a_finished_resume_is_prepared_next(conn):
    with conn:
        scored(conn, add_job(conn, "Unwritten"), score=85)
    counts = gather(conn)
    assert (counts["ready"], counts["unwritten"]) == (0, 1)
    assert counts["next"] == ("Prepare 1 job ready for preparation", "/ready")


def test_the_pipeline_counts_each_stage_reached_by_jobs_found_in_30_days(conn):
    with conn:
        scored(conn, add_job(conn, "Scored", **found(MONTH)), score=40)
        written(conn, "Ready", **found(TODAY))
        for stage in ("applied", "interview", "rejected"):
            job = add_job(conn, stage, **found(TODAY))
            version(conn, job)
            conn.execute(CHANGED, (job, stage, "1" if stage == "interview" else None, "2026-11-10", utc(TODAY, 8)))
        add_job(conn, "Too old", **found(MONTH - timedelta(1)))
    counts = gather(conn)
    pipeline = [counts[name] for name in ("collected", "p_scored", "p_written", "p_ready", "p_applied", "p_replied",
                                          "p_rejected")]  # fmt: skip
    assert pipeline == [5, 2, 4, 1, 3, 1, 1]
    assert (counts["applied"], counts["replies"], counts["replies_week"]) == (3, 1, 1)


def test_waiting_leaves_out_failed_scores_and_quick_check_turn_downs(conn):
    with conn:
        scored(conn, add_job(conn, "Waiting"))
        scored(conn, add_job(conn, "Quick"), passed=0)
        scored(conn, add_job(conn, "Failed"), failed="AI score failed twice")
    counts = gather(conn)
    assert (counts["waiting"], counts["failed"], counts["scored"]) == (1, 1, 0)


@pytest.mark.parametrize(("first_seen", "week", "month", "in_90"), [
    (utc(WEEK, 0, 0, 0), 1, 1, 1), (utc(WEEK - timedelta(1), 23, 59, 59), 0, 1, 1),
    (utc(MONTH, 0, 0, 0), 0, 1, 1), (utc(MONTH - timedelta(1), 23, 59, 59), 0, 0, 1),
    (utc(FIRST, 0, 0, 0), 0, 0, 1), (utc(FIRST - timedelta(1), 23, 59, 59), 0, 0, 0),
])  # fmt: skip
def test_windows_start_at_local_midnight(conn, first_seen, week, month, in_90):
    with conn:
        add_job(conn, "Edge", first_seen=first_seen)
    counts = gather(conn)
    assert (counts["open_week"], counts["charts"][7]["collected"]) == (week, week)
    assert (counts["collected"], counts["charts"][30]["collected"], counts["charts"][90]["collected"]) == (
        month,
        month,
        in_90,
    )


def test_local_days_follow_this_computer_s_time_zone(conn, phoenix):
    with conn:  # Phoenix is 7 hours behind UTC: its 6 November starts at 07:00 UTC
        add_job(conn, "Week's first second", first_seen="2026-11-06T07:00:00+00:00")
        add_job(conn, "The evening before", first_seen="2026-11-06T06:59:59+00:00")
    counts = gather(conn)
    assert counts["open_week"] == 1
    assert [column["collected"] for column in counts["charts"][30]["columns"][-8:-6]] == [1, 1]  # 5 and 6 November


def test_badges_and_sent_count_this_week_from_today_and_the_6_days_before(conn):
    with conn:
        for applied, moved in ((WEEK, utc(WEEK, 0, 0, 0)), (WEEK - timedelta(1), utc(WEEK - timedelta(1), 23, 59, 59))):
            job = add_job(conn, "Applied")
            conn.execute(CHANGED, (job, "offer", None, str(applied), moved))
    counts = gather(conn)
    assert (counts["applied"], counts["applied_week"], counts["replies"], counts["replies_week"]) == (2, 1, 2, 1)
    assert (counts["charts"][7]["sent"], counts["charts"][30]["sent"]) == (1, 2)
    assert [row[3] for row in counts["moved"]] == ["Offer"]


def test_90_days_are_13_weeks_ending_today(conn):
    with conn:
        add_job(conn, "First day", **found(FIRST))
        add_job(conn, "Second week", **found(FIRST + timedelta(7)))
        job = add_job(conn, "Applied", **found(TODAY))
        conn.execute(CHANGED, (job, "applied", None, str(TODAY), utc(TODAY, 8)))
    chart = gather(conn)["charts"][90]
    assert len(chart["columns"]) == 13
    assert (chart["columns"][0]["start"], chart["columns"][-1]["end"]) == (FIRST, TODAY)
    assert [column["collected"] for column in chart["columns"]][:2] == [1, 1]
    assert (chart["collected"], chart["sent"], chart["per_100"]) == (3, 1, 33)


@pytest.mark.parametrize(("scores", "middle"), [([40], 40), ([90, 40, 70], 70), ([40, 71, 90, 70], 71)])
def test_the_middle_score_of_open_jobs(conn, scores, middle):
    with conn:
        for score in scores:
            scored(conn, add_job(conn, "Scored"), score=score)
        scored(conn, add_job(conn, "Closed", status="closed"), score=0)
    counts = gather(conn)
    assert counts["median"] == middle and counts["scored"] == len(scores)


def test_passed_and_scored_today_use_the_local_day(conn):
    with conn:
        for score, on in (
            (85, utc(TODAY, 0, 0, 0)),
            (40, utc(TODAY, 23, 0)),
            (90, utc(TODAY - timedelta(1), 23, 59, 59)),
        ):
            scored(conn, job := add_job(conn, "Scored"), score=score)
            conn.execute("UPDATE ai_scores SET scored_on = ? WHERE job_id = ?", (on, job))
    counts = gather(conn)
    assert (counts["passed"], counts["scored_today"], counts["passed_today"]) == (2, 2, 1)


def test_rejected_replies_show_as_not_moving_on(conn):
    with conn:
        conn.execute(CHANGED, (add_job(conn, "Turned down"), "rejected", None, "2026-11-01", utc(TODAY, 8)))
    assert [row[3] for row in gather(conn)["moved"]] == ["Not moving on"]


def test_the_page_needs_a_login_and_has_no_form_of_its_own(client):
    assert client.get("/summary", follow_redirects=False).headers["location"] == "/login"
    assert "<form" not in (main.HERE / "templates" / "summary.html").read_text()


def test_sign_in_lands_on_summary(client):
    assert log_in(client).headers["location"] == "/summary"


def test_the_page_says_what_needs_attention_and_without_gmail_how_replies_count(client, conn, monkeypatch):
    monkeypatch.setattr(main, "_scoring", lambda: CHOSEN)
    log_in(client)
    with conn:
        written(conn)
    text = client.get("/summary").text
    assert "1 application is ready for your review." in text
    assert '<a class="button primary" href="/ready">Review 1 ready application</a>' in text
    assert "Gmail isn't connected" in text


def test_aa_opens_summary_and_the_phone_menu_starts_with_it(client):
    log_in(client)
    text = client.get("/summary").text
    assert re.search(r'<a class="brand-mark" href="/summary" aria-current="page"><span aria-hidden="true">aa</span>'
                     r'<span class="sr-only">Summary</span></a>', text)  # fmt: skip
    rail, sheet = re.findall(r'<nav class="(?:rail|sheet)" aria-label="Pages">(.*?)</nav>', text, re.S)
    assert '<a href="/summary"' not in rail  # no extra rail row: "aa" is the way there
    assert re.findall(r'<a href="([^"]+)"', sheet)[0] == "/summary"
    assert re.search(r'<span class="caption">Overview</span><strong>Summary</strong>', text)
    assert 'class="brand-mark" href="/summary">' in client.get("/").text  # not current elsewhere
