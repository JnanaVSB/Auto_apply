"""The Tracker (Phase 5): where each job with a finished resume version is on the board, Ready to Offer or Rejected,
and every move. A move made by hand is recorded as yours, so Phase 6 (Gmail) can leave it alone."""

from datetime import date

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
