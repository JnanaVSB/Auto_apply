from datetime import date

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
