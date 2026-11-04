"""One Gmail sync pass: read the e-mails that are new since the last pass (each only once), sort and match them, move
their cards, and keep the ones it can't place for you to check. The caller holds the lock and builds the client."""

import json
from datetime import datetime, timedelta

import sqlcipher3

from app.gmail import apply, classify, match
from app.gmail.client import Gmail, GmailError, HistoryTooOld

# Gmail's own search only narrows what is read ({…} means "any of"); the rules decide what each e-mail is.
QUERY = (
    "{since} {{"
    + " ".join(f"from:{system}" for system in classify.JOB_SYSTEMS)
    + " subject:application subject:applying subject:interview subject:assessment subject:offer"
    " subject:unfortunately}}"
)
THREAD = "SELECT job_id FROM emails WHERE thread_id = ? AND job_id IS NOT NULL ORDER BY received_on DESC LIMIT 1"
SAVE = """INSERT INTO emails (message_id, thread_id, received_on, sender_domain, label, labelled_by, job_id, moved,
    to_check, subject, candidates) VALUES (?, ?, ?, ?, ?, 'rules', ?, ?, ?, ?, ?)"""
RESULT = "UPDATE gmail_sync SET last_sync_on = ?, last_result = ? WHERE id = 1"


def run(conn: sqlcipher3.Connection, gmail: Gmail, lookback_days: int, now: datetime) -> str:
    """One pass; returns its result in plain words (also saved). A GmailError is saved as the result and raised again;
    the e-mails done before it stay done."""
    with conn:
        conn.execute("INSERT OR IGNORE INTO gmail_sync (id) VALUES (1)")
    history_id, last_sync = conn.execute("SELECT history_id, last_sync_on FROM gmail_sync WHERE id = 1").fetchone()
    at = now.isoformat(timespec="seconds")
    read = moved = to_check = 0
    try:
        ids, newest = _new_ids(gmail, history_id, last_sync, lookback_days, now)
        cards = match.cards(conn)
        for message_id in ids:
            if conn.execute("SELECT 1 FROM emails WHERE message_id = ?", (message_id,)).fetchone():
                continue  # each e-mail is read once
            found = _one(conn, gmail.message(message_id), cards)
            read, moved, to_check = read + 1, moved + found[0], to_check + found[1]
    except GmailError as error:
        with conn:
            conn.execute(RESULT, (at, str(error)))
        raise
    result = f"Read {_count(read, 'new e-mail')}: {_count(moved, 'card')} moved, {to_check} to check."
    with conn:
        conn.execute("UPDATE gmail_sync SET history_id = ? WHERE id = 1", (newest,))
        conn.execute(RESULT, (at, result))
    return result


def _new_ids(gmail: Gmail, history_id: str | None, last_sync: str | None, days: int, now: datetime) -> tuple[list, str]:
    """The ids to look at and the position to save. The position is taken before a search, so nothing that arrives
    during the search is missed (the next pass reads it from there)."""
    if history_id:
        try:
            return gmail.history(history_id)
        except HistoryTooOld:  # too long without a sync: search again from the day before the last one (Gmail reads
            # the date in the account's own time zone; e-mails already saved are skipped anyway)
            since = datetime.fromisoformat(last_sync) - timedelta(days=1) if last_sync else now - timedelta(days=days)
            newest = gmail.profile()[1]
            return gmail.search(QUERY.format(since=f"after:{since:%Y/%m/%d}")), newest
    newest = gmail.profile()[1]
    return gmail.search(QUERY.format(since=f"newer_than:{days}d")), newest


def _one(conn: sqlcipher3.Connection, message: dict, cards: list[dict]) -> tuple[bool, bool]:
    """Sort, match and save one e-mail, moving its card first; whether it moved a card and whether it is to check."""
    name, domain = classify.sender(message["from"])
    label = classify.classify(domain, message["subject"], message["snippet"])
    thread = conn.execute(THREAD, (message["thread_id"],)).fetchone()
    found = match.match(cards, name, domain, message["subject"], thread[0] if thread else None)
    if label == "other" and len(found) == 1:
        label = "unclear"  # a company you applied to wrote and no phrase fits: the local model decides
    job, moved, check = (found[0], False, False) if len(found) == 1 else (None, False, label != "other")
    if job is not None:
        moved = apply.update(conn, job, label, datetime.fromisoformat(message["received_on"]))
    with conn:  # saved after its card: a crash in between only reads it again, and a card never moves back
        conn.execute(SAVE, (
            message["id"], message["thread_id"], message["received_on"], domain, label, job, int(moved), int(check),
            message["subject"] if check else None, json.dumps(found if check else []),
        ))  # fmt: skip
    return moved, check


def _count(number: int, word: str) -> str:
    return f"{number} {word}{'' if number == 1 else 's'}"
