"""What a matched e-mail does to its card (forward only, never over a move of yours), and the e-mails waiting for you
on E-mails to check. A matched e-mail's row also keeps its card from greying out (tracker.board reads the newest)."""

import json
from datetime import datetime

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
YOURS = "SELECT max(moved_on) FROM tracker_moves WHERE job_id = ? AND moved_by = 'you'"
DECIDED = """UPDATE emails SET job_id = ?, label = ?, labelled_by = 'you', moved = ?, to_check = 0, subject = NULL,
    candidates = '[]' WHERE message_id = ?"""
DISMISSED = "UPDATE emails SET to_check = 0, subject = NULL, candidates = '[]' WHERE message_id = ?"
TO_CHECK = """SELECT message_id, received_on, sender_domain, subject, label, candidates FROM emails WHERE to_check = 1
    ORDER BY received_on DESC"""


def update(conn: sqlcipher3.Connection, job_id: int, label: str, received_on: datetime) -> bool:
    """Move the card if the e-mail's label says so (`received_on`: aware); whether it moved."""
    target, current = TARGET.get(label), tracker.stage(conn, job_id) or "ready"
    forward = target in FORWARD and current in FORWARD and FORWARD.index(target) > FORWARD.index(current)
    if not (forward or (target == "rejected" and current in FORWARD[:-1])):
        return False
    yours = conn.execute(YOURS, (job_id,)).fetchone()[0]
    if yours and datetime.fromisoformat(yours) > received_on:  # you moved it after this e-mail: you win
        return False
    return tracker.move(conn, job_id, current, target, received_on.astimezone().date(), moved_by="gmail")


def decide(conn: sqlcipher3.Connection, message_id: str, job_id: int, label: str) -> bool:
    """You said which card an e-mail on the list is about and what it is: it moves that card as the rules would (only
    forward, never over a move of yours) and leaves the list, its subject forgotten. Whether the card moved."""
    received_on = conn.execute("SELECT received_on FROM emails WHERE message_id = ?", (message_id,)).fetchone()[0]
    moved = update(conn, job_id, label, datetime.fromisoformat(received_on))
    with conn:
        conn.execute(DECIDED, (job_id, label, int(moved), message_id))
    return moved


def dismiss(conn: sqlcipher3.Connection, message_id: str) -> None:
    """Off the list, its subject forgotten; nothing else changes."""
    with conn:
        conn.execute(DISMISSED, (message_id,))


def to_check(conn: sqlcipher3.Connection) -> list[dict]:
    """The e-mails waiting for you, newest first, with the job ids each may be about."""
    return [
        {"id": message_id, "when": _local(received_on), "day": datetime.fromisoformat(received_on).astimezone().date(),
         "domain": domain, "subject": subject, "label": label, "candidates": json.loads(candidates)}
        for message_id, received_on, domain, subject, label, candidates in conn.execute(TO_CHECK)
    ]  # fmt: skip


def on_list(conn: sqlcipher3.Connection, message_id: str) -> bool:
    """Whether this e-mail waits for you to check it."""
    return (
        conn.execute("SELECT 1 FROM emails WHERE message_id = ? AND to_check = 1", (message_id,)).fetchone() is not None
    )


def waiting(conn: sqlcipher3.Connection) -> int:
    """How many e-mails wait for you to check."""
    return conn.execute("SELECT count(*) FROM emails WHERE to_check = 1").fetchone()[0]


def of_job(conn: sqlcipher3.Connection, job_id: int) -> list[dict]:
    """The job's e-mails, newest first: when (this computer's time), what it was and who sent it."""
    rows = conn.execute(
        "SELECT received_on, label, sender_domain FROM emails WHERE job_id = ? ORDER BY received_on DESC", (job_id,)
    )
    return [{"when": _local(received_on), "label": label, "domain": domain} for received_on, label, domain in rows]


def _local(at: str) -> str:
    return f"{datetime.fromisoformat(at).astimezone():%Y-%m-%d %H:%M}"
