"""How a matched e-mail moves its card (made-up jobs and e-mails)."""

from datetime import UTC, datetime, timedelta

import pytest
from helpers import add_job
from test_tracker import DAY, card, moves, place, version

from app import tracker
from app.gmail.apply import update

EARLY = datetime(2026, 11, 4, 3, 0, tzinfo=UTC)  # 3 November, 20:00 in Phoenix


@pytest.fixture
def ready(conn):
    """A Ready card: a job with a finished version and no place on the Tracker yet."""
    with conn:
        job = add_job(conn, "Robotics Intern")
        version(conn, job)
    return job


def test_a_confirmation_moves_a_ready_card_to_applied_on_the_e_mail_s_local_date(conn, ready, phoenix):
    assert update(conn, ready, "confirmation", EARLY)
    assert card(conn, ready) == ("applied", None, "2026-11-03")
    assert tracker.history(conn, ready)[0]["moved_by"] == "gmail"


@pytest.mark.parametrize("start, label, end", [("applied", "interview", "interview"), ("interview", "offer", "offer")])
def test_an_e_mail_moves_a_card_forward(conn, start, label, end):
    with conn:
        job = place(conn, start, round_="2" if start == "interview" else None)
    assert update(conn, job, label, EARLY) and tracker.stage(conn, job) == end
    assert card(conn, job)[1] == ("1" if end == "interview" else None)


@pytest.mark.parametrize(
    "start, label", [("interview", "confirmation"), ("offer", "rejection"), ("rejected", "interview")]
)
def test_an_e_mail_never_moves_a_card_back_or_out_of_an_outcome(conn, start, label):
    with conn:
        job = place(conn, start, round_="2" if start == "interview" else None)
    assert not update(conn, job, label, EARLY) and tracker.stage(conn, job) == start


@pytest.mark.parametrize("start", ["applied", "assessment", "interview"])
def test_a_rejection_moves_a_waiting_card_to_rejected(conn, start):
    with conn:
        job = place(conn, start, round_="2" if start == "interview" else None)
    assert update(conn, job, "rejection", EARLY) and tracker.stage(conn, job) == "rejected"


@pytest.mark.parametrize("label", ["other", "unclear"])
def test_other_and_unclear_move_nothing(conn, label):
    with conn:
        job = place(conn, "applied")
    assert not update(conn, job, label, EARLY) and tracker.stage(conn, job) == "applied"


def test_a_move_of_yours_after_the_e_mail_wins(conn, ready):
    tracker.move(conn, ready, "ready", "applied", DAY)  # now, after EARLY
    assert not update(conn, ready, "interview", EARLY) and tracker.stage(conn, ready) == "applied"
    assert update(conn, ready, "interview", datetime.now(UTC) + timedelta(minutes=1))
    assert tracker.stage(conn, ready) == "interview" and moves(conn, ready) == 2
