"""The Tracker (Phase 5): where each job with a finished resume version is on the board, Ready to Offer or Rejected,
and every move. A move made by hand is recorded as yours, so Phase 6 (Gmail) can leave it alone."""

from datetime import date, datetime, timedelta
from urllib.parse import urlsplit

import sqlcipher3

from app.collect import now

STAGES = {  # board order
    "ready": "Ready",
    "applied": "Applied",
    "assessment": "Online Assessment",
    "interview": "Interview",
    "offer": "Offer",
    "rejected": "Rejected",
}
ROUNDS = {"1": "Round 1", "2": "Round 2", "3": "Round 3", "4": "Round 4", "5": "Round 5", "final": "Final"}
WAITING = ("applied", "assessment", "interview")  # the stages whose cards go grey when nothing changes for a while
LANE_CARDS = 30  # cards a lane shows before "Show all"
# Every card, newest change first. A card past Ready always shows, whatever "Not interested", closed or its versions;
# Ready holds a job with a finished resume version, or one you moved back, unless it is "Not interested".
BOARD = """SELECT j.id, j.company, j.title, j.apply_url, j.status, coalesce(t.stage, 'ready'), t.round, t.applied_on,
        t.changed_on, coalesce(t.changed_on, f.done_on) AS sort_on
    FROM jobs j LEFT JOIN tracker t ON t.job_id = j.id
    LEFT JOIN (SELECT job_id, max(coalesce(finished_on, created_on)) AS done_on FROM preparations
        WHERE kind = 'resume' AND status = 'done' GROUP BY job_id) f ON f.job_id = j.id
    WHERE t.stage != 'ready' OR (j.not_interested = 0 AND (t.job_id IS NOT NULL OR f.job_id IS NOT NULL))
    ORDER BY sort_on DESC, j.id DESC"""
# One statement, so the check and the write can't be split by another tab's move: a job with no row is in Ready, so a
# row is added only when the page showed Ready; an existing row moves only if it is still where the page showed it.
# The day counter's date is set on leaving Ready, kept between later stages and cleared back in Ready.
MOVE = """INSERT INTO tracker (job_id, stage, round, applied_on, changed_on)
    SELECT :job, :to, :round, :applied, :now
    WHERE :expected = 'ready' OR EXISTS (SELECT 1 FROM tracker WHERE job_id = :job)
    ON CONFLICT (job_id) DO UPDATE SET stage = excluded.stage, round = excluded.round,
        applied_on = iif(excluded.stage = 'ready', NULL, coalesce(tracker.applied_on, excluded.applied_on)),
        changed_on = excluded.changed_on
    WHERE tracker.stage = :expected"""
ADD_MOVE = """INSERT INTO tracker_moves (job_id, from_stage, to_stage, round, moved_by, moved_on)
    VALUES (?, ?, ?, ?, ?, ?)"""


def move(conn: sqlcipher3.Connection, job_id: int, expected: str, to: str, on: date, moved_by: str = "you") -> bool:
    """Move the card from `expected` (the stage the page showed) to `to`, on the local date `on`, and record it.
    False when it was moved meanwhile (another tab). The caller checks the job exists."""
    if expected not in STAGES or to not in STAGES or expected == to:
        raise ValueError("Choose two different stages.")
    round_ = "1" if to == "interview" else None
    applied = None if to == "ready" else on.isoformat()
    at = now()
    with conn:
        values = {"job": job_id, "to": to, "round": round_, "applied": applied, "now": at, "expected": expected}
        if conn.execute(MOVE, values).rowcount != 1:
            return False
        conn.execute(ADD_MOVE, (job_id, expected, to, round_, moved_by, at))
    return True


def set_round(conn: sqlcipher3.Connection, job_id: int, round_: str) -> bool:
    """Change an Interview card's round and record it; False when the card isn't in Interview or has that round."""
    if round_ not in ROUNDS:
        raise ValueError("Choose a round from the list.")
    at = now()
    with conn:
        changed = conn.execute(
            "UPDATE tracker SET round = ?, changed_on = ? WHERE job_id = ? AND stage = 'interview' AND round != ?",
            (round_, at, job_id, round_),
        ).rowcount
        if changed != 1:
            return False
        conn.execute(ADD_MOVE, (job_id, "interview", "interview", round_, "you", at))
    return True


def history(conn: sqlcipher3.Connection, job_id: int) -> list[dict]:
    """The card's moves, newest first."""
    rows = conn.execute(
        "SELECT from_stage, to_stage, round, moved_by, moved_on FROM tracker_moves WHERE job_id = ? ORDER BY id DESC",
        (job_id,),
    )
    return [dict(zip(("from_stage", "to_stage", "round", "moved_by", "moved_on"), row, strict=True)) for row in rows]


def stage(conn: sqlcipher3.Connection, job_id: int) -> str | None:
    """The card's stage, or None when it has no row (a job with a finished version is then in Ready)."""
    row = conn.execute("SELECT stage FROM tracker WHERE job_id = ?", (job_id,)).fetchone()
    return row[0] if row else None


def board(conn: sqlcipher3.Connection, now: datetime, stale_days: int, show_all: str | None = None) -> list[dict]:
    """The lanes in board order, each {"stage", "name", "cards", "count", "more"}; a lane shows its newest
    LANE_CARDS cards unless it is `show_all`. `now` is this computer's local time, aware: the day counter counts local
    dates, and a waiting card goes grey when it hasn't changed for `stale_days`."""
    today, cutoff = now.date(), now - timedelta(days=stale_days)
    lanes = {stage: [] for stage in STAGES}
    for job_id, company, title, url, status, stage_, round_, applied_on, changed_on, _ in conn.execute(BOARD):
        lanes[stage_].append(
            {
                "id": job_id,
                "company": company,
                "title": title,
                "site": (urlsplit(url).hostname or "").removeprefix("www."),
                "closed": status != "open",
                "round": round_,
                "days": None if applied_on is None else max(0, (today - date.fromisoformat(applied_on)).days),
                "stale": stage_ in WAITING and datetime.fromisoformat(changed_on) < cutoff,
            }
        )
    return [
        {
            "stage": stage_,
            "name": name,
            "cards": lanes[stage_] if show_all == stage_ else lanes[stage_][:LANE_CARDS],
            "count": len(lanes[stage_]),
            "more": stage_ != show_all and len(lanes[stage_]) > LANE_CARDS,
        }
        for stage_, name in STAGES.items()
    ]
