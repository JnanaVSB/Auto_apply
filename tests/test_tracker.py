from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from helpers import add_job, one

from app import tracker

DAY = date(2026, 11, 4)


@pytest.fixture
def job(conn):
    with conn:
        return add_job(conn, "Robotics Intern")


def moves(conn, job):
    return one(conn, "SELECT count(*) FROM tracker_moves WHERE job_id = ?", job)[0]


def card(conn, job):
    return one(conn, "SELECT stage, round, applied_on FROM tracker WHERE job_id = ?", job)


def test_i_applied_starts_the_day_counter_and_records_the_move(conn, job):
    assert tracker.stage(conn, job) is None
    assert tracker.move(conn, job, "ready", "applied", DAY)
    assert card(conn, job) == ("applied", None, "2026-11-04")
    assert [(m["from_stage"], m["to_stage"], m["moved_by"]) for m in tracker.history(conn, job)] == [
        ("ready", "applied", "you")
    ]


def test_a_second_tab_s_same_move_changes_nothing(conn, job):
    assert tracker.move(conn, job, "ready", "applied", DAY)
    assert not tracker.move(conn, job, "ready", "applied", date(2026, 11, 5))
    assert card(conn, job) == ("applied", None, "2026-11-04") and moves(conn, job) == 1


def test_later_moves_keep_the_date_and_set_or_clear_the_round(conn, job):
    tracker.move(conn, job, "ready", "applied", DAY)
    assert tracker.move(conn, job, "applied", "interview", date(2026, 11, 20))
    assert card(conn, job) == ("interview", "1", "2026-11-04")
    assert tracker.move(conn, job, "interview", "offer", date(2026, 11, 30))
    assert card(conn, job) == ("offer", None, "2026-11-04")


def test_back_to_ready_clears_the_date_and_keeps_the_history(conn, job):
    tracker.move(conn, job, "ready", "applied", DAY)
    assert tracker.move(conn, job, "applied", "ready", DAY)
    assert card(conn, job) == ("ready", None, None)
    assert [m["to_stage"] for m in tracker.history(conn, job)] == ["ready", "applied"]  # newest first


def test_the_round_changes_only_on_an_interview_card_and_is_recorded(conn, job):
    tracker.move(conn, job, "ready", "applied", DAY)
    assert not tracker.set_round(conn, job, "2")  # not in Interview
    tracker.move(conn, job, "applied", "interview", DAY)
    assert tracker.set_round(conn, job, "final")
    assert not tracker.set_round(conn, job, "final")  # the same round again
    assert card(conn, job)[1] == "final"
    assert tracker.history(conn, job)[0] | {"moved_on": None} == {
        "from_stage": "interview", "to_stage": "interview", "round": "final", "moved_by": "you", "moved_on": None
    }  # fmt: skip
    assert moves(conn, job) == 3


@pytest.mark.parametrize("expected, to", [("ready", "ghosted"), ("waiting", "applied"), ("applied", "applied")])
def test_an_unknown_or_unchanged_stage_is_refused(conn, job, expected, to):
    with pytest.raises(ValueError):
        tracker.move(conn, job, expected, to, DAY)


def test_an_unknown_round_is_refused(conn, job):
    with pytest.raises(ValueError):
        tracker.set_round(conn, job, "6")


def test_a_card_not_on_the_board_is_not_where_the_page_showed_it(conn, job):
    assert not tracker.move(conn, job, "applied", "interview", DAY)
    assert tracker.stage(conn, job) is None and moves(conn, job) == 0


NOW = datetime(2026, 11, 4, 23, 30, tzinfo=timezone(timedelta(hours=-7)))  # already 5 November in UTC
CHANGED = "INSERT INTO tracker (job_id, stage, round, applied_on, changed_on) VALUES (?, ?, ?, ?, ?)"


def version(conn, job, status="done", on="2026-11-01T10:00:00+00:00"):
    conn.execute(
        "INSERT INTO preparations (job_id, kind, version, sources, writer, status, draft, created_on, finished_on) "
        "VALUES (?, 'resume', 1, '[]', '{}', ?, '{}', ?, ?)",
        (job, status, on, on),
    )


def place(conn, stage, days_ago=0, applied_on="2026-11-01", round_=None, **job):
    """A made-up job already on the board, last changed `days_ago` days before NOW."""
    job_id = add_job(conn, "Robotics Intern", **job)
    changed = (NOW - timedelta(days=days_ago)).astimezone(UTC).isoformat(timespec="seconds")
    conn.execute(CHANGED, (job_id, stage, round_, None if stage == "ready" else applied_on, changed))
    return job_id


def lane(conn, stage, stale_days=21, show_all=None):
    return next(each for each in tracker.board(conn, NOW, stale_days, show_all) if each["stage"] == stage)


def ids(conn, stage):
    return [card["id"] for card in lane(conn, stage)["cards"]]


def test_ready_holds_jobs_with_a_finished_version_unless_not_interested(conn):
    with conn:
        done, by_hand, skipped = (add_job(conn, title) for title in ("Done", "By hand", "Skipped"))
        version(conn, done)
        version(conn, by_hand, "by_hand")
        version(conn, skipped)
        conn.execute("UPDATE jobs SET not_interested = 1 WHERE id = ?", (skipped,))
    assert ids(conn, "ready") == [done]


def test_a_card_past_ready_shows_even_when_not_interested_and_closed(conn):
    with conn:
        job = place(conn, "applied", not_interested=1, status="closed")
    assert [card["closed"] for card in lane(conn, "applied")["cards"]] == [True] and ids(conn, "applied") == [job]


def test_a_card_moved_back_to_ready_stays_there_without_a_version(conn, job):
    tracker.move(conn, job, "ready", "applied", DAY)
    tracker.move(conn, job, "applied", "ready", DAY)
    assert ids(conn, "ready") == [job] and lane(conn, "ready")["cards"][0]["days"] is None


def test_the_newest_change_comes_first(conn):
    with conn:
        older, newer = place(conn, "offer", days_ago=3), place(conn, "offer", days_ago=1)
    assert ids(conn, "offer") == [newer, older]


@pytest.mark.parametrize("applied_on, days", [("2026-11-04", 0), ("2026-10-23", 12), ("2026-11-10", 0)])
def test_the_day_counter_counts_local_days_since_applied(conn, applied_on, days):
    with conn:
        place(conn, "applied", applied_on=applied_on)
    assert lane(conn, "applied")["cards"][0]["days"] == days


@pytest.mark.parametrize(
    "stage, days_ago, stale",
    [("applied", 22, True), ("interview", 22, True), ("applied", 20, False), ("offer", 100, False),
     ("rejected", 100, False), ("ready", 100, False)],
)  # fmt: skip
def test_a_waiting_card_goes_grey_after_the_set_days_without_a_change(conn, stage, days_ago, stale):
    with conn:
        place(conn, stage, days_ago, round_="2" if stage == "interview" else None)
    assert lane(conn, stage)["cards"][0]["stale"] is stale


def test_a_long_lane_shows_its_newest_cards_until_show_all(conn):
    with conn:
        for _ in range(tracker.LANE_CARDS + 1):
            place(conn, "applied")
    cut, whole = lane(conn, "applied"), lane(conn, "applied", show_all="applied")
    assert (len(cut["cards"]), cut["count"], cut["more"]) == (30, 31, True)
    assert (len(whole["cards"]), whole["count"], whole["more"]) == (31, 31, False)


def test_the_site_is_the_link_s_host_without_www(conn):
    with conn:
        place(conn, "applied", apply_url="https://www.careers.acme.example/jobs/1")
    assert lane(conn, "applied")["cards"][0]["site"] == "careers.acme.example"
