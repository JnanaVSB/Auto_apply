"""What a matched e-mail does to its card: moves it forward (never back, never over a move of yours) and keeps it from
greying out."""

from datetime import UTC, datetime

import sqlcipher3

from app import tracker

TARGET = {  # where each label sends a card; "other" and "unclear" move nothing
    "confirmation": "applied",
    "assessment": "assessment",
    "interview": "interview",
    "offer": "offer",
    "rejection": "rejected",
}
# Gmail only moves a card along this line, or to Rejected from any stage before Offer; never back, never out of Offer
# or Rejected.
FORWARD = ("ready", "applied", "assessment", "interview", "offer")
LATEST = "UPDATE tracker SET last_email_on = max(coalesce(last_email_on, ?), ?) WHERE job_id = ?"
YOURS = "SELECT max(moved_on) FROM tracker_moves WHERE job_id = ? AND moved_by = 'you'"


def update(conn: sqlcipher3.Connection, job_id: int, label: str, received_on: datetime) -> bool:
    """Note the e-mail on the card and move the card if its label says so (`received_on`: aware); whether it moved."""
    at = received_on.astimezone(UTC).isoformat(timespec="seconds")  # like changed_on, so the two compare as text
    with conn:  # only a card on the Tracker has a row; an untouched Ready card has nothing to note
        conn.execute(LATEST, (at, at, job_id))
    target, current = TARGET.get(label), tracker.stage(conn, job_id) or "ready"
    forward = target in FORWARD and current in FORWARD and FORWARD.index(target) > FORWARD.index(current)
    if not (forward or (target == "rejected" and current in FORWARD[:-1])):
        return False
    yours = conn.execute(YOURS, (job_id,)).fetchone()[0]
    if yours and datetime.fromisoformat(yours) > received_on:  # you moved it after this e-mail: you win
        return False
    return tracker.move(conn, job_id, current, target, received_on.astimezone().date(), moved_by="gmail")
