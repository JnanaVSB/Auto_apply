"""One Gmail sync pass: read the e-mails that are new since the last pass (each only once), sort and match them, move
their cards, and keep the ones it can't place for you to check. The caller holds the lock and builds the client."""

import json
from collections.abc import Callable
from datetime import datetime, timedelta

import sqlcipher3

from app import tracker
from app.config import AIRole
from app.gmail import apply, classify, match, model
from app.gmail.client import Gmail, GmailError, Gone, HistoryTooOld

# Gmail's own search only narrows what is read ({…} means "any of"); the rules decide what each e-mail is. Your own
# mail, drafts, spam, bin and chats are never read.
QUERY = (
    "{since} -in:sent -in:drafts -in:spam -in:trash -in:chats {{"
    + " ".join(f"from:{system}" for system in classify.JOB_SYSTEMS)
    + " subject:application subject:applying subject:interview subject:assessment subject:offer"
    " subject:unfortunately}}"
)
SKIPPED = {"SENT", "DRAFT", "SPAM", "TRASH", "CHAT"}  # Gmail's labels for mail that is never sorted (history lists it)
THREAD = "SELECT job_id FROM emails WHERE thread_id = ? AND job_id IS NOT NULL ORDER BY received_on DESC LIMIT 1"
SAVE = """INSERT INTO emails (message_id, thread_id, received_on, sender_domain, label, labelled_by, job_id, moved,
    to_check, subject, candidates) VALUES (?, ?, ?, ?, ?, 'rules', ?, ?, ?, ?, ?)"""
RESULT = "UPDATE gmail_sync SET last_result = ? WHERE id = 1"
DONE = "UPDATE gmail_sync SET history_id = ?, last_sync_on = ?, last_result = ? WHERE id = 1"  # a good pass only
MODEL_BATCH = 20  # unclear e-mails the local model sorts per pass
# Still on your list (one you dismissed is left alone), newest first: new mail matters most for the board, and an old
# one the model never answers stays on the list for you
UNCLEAR = """SELECT message_id, subject, job_id, received_on FROM emails WHERE label = 'unclear' AND to_check = 1
    ORDER BY received_on DESC LIMIT ?"""
SORTED_OFF = """UPDATE emails SET label = ?, labelled_by = 'local model', moved = ?, to_check = 0, subject = NULL,
    candidates = '[]' WHERE message_id = ?"""
SORTED = "UPDATE emails SET label = ?, labelled_by = 'local model' WHERE message_id = ?"


def run(
    conn: sqlcipher3.Connection,
    gmail: Gmail,
    lookback_days: int,
    now: datetime,
    role: AIRole | None = None,
    scoring: Callable[[], bool] = lambda: False,
) -> str:
    """One pass; returns its result in plain words (also saved). Then the local model (`role`, Ollama only) sorts the
    unclear e-mails, each only while no scoring run is using the graphics card. A GmailError is saved as the result
    and raised again; the e-mails done before it stay done, and the next pass starts from the last good one."""
    with conn:
        conn.execute("INSERT OR IGNORE INTO gmail_sync (id) VALUES (1)")
    history_id, last_sync = conn.execute("SELECT history_id, last_sync_on FROM gmail_sync WHERE id = 1").fetchone()
    read = moved = to_check = 0
    try:
        ids, newest = _new_ids(gmail, history_id, last_sync, lookback_days, now)
        cards = match.cards(conn)
        for message_id in ids:
            if conn.execute("SELECT 1 FROM emails WHERE message_id = ?", (message_id,)).fetchone():
                continue  # each e-mail is read once
            try:
                message = gmail.message(message_id)
            except Gone:  # deleted meanwhile, or a draft Gmail replaced: nothing to read
                continue
            if SKIPPED.intersection(message["labels"]):
                continue
            found = _one(conn, message, cards)
            read, moved, to_check = read + 1, moved + found[0], to_check + found[1]
        local = role is not None and role.service == "ollama"  # an e-mail's text never goes to a cloud model
        by_model = _sort_unclear(conn, gmail, role, scoring) if local else 0
    except GmailError as error:
        with conn:
            conn.execute(RESULT, (str(error),))
        raise
    result = f"Read {_count(read, 'new e-mail')}: {_count(moved, 'card')} moved, {to_check} to check"
    result += f", {by_model} sorted by the local model." if by_model else "."
    with conn:
        conn.execute(DONE, (newest, now.isoformat(timespec="seconds"), result))
    return result


def _new_ids(gmail: Gmail, history_id: str | None, last_sync: str | None, days: int, now: datetime) -> tuple[list, str]:
    """The ids to look at and the position to save. The position is taken before a search, so nothing that arrives
    during the search is missed (the next pass reads it from there)."""
    if history_id:
        try:
            return gmail.history(history_id)
        except HistoryTooOld:  # too long without a sync: search again from the day before the last good one (Gmail
            # reads the date in the account's own time zone; e-mails already saved are skipped anyway)
            since = datetime.fromisoformat(last_sync) - timedelta(days=1) if last_sync else now - timedelta(days=days)
            newest = gmail.profile()[1]
            return gmail.search(QUERY.format(since=f"after:{since:%Y/%m/%d}")), newest
    newest = gmail.profile()[1]
    return gmail.search(QUERY.format(since=f"newer_than:{days}d")), newest


def _one(conn: sqlcipher3.Connection, message: dict, cards: list[dict]) -> tuple[bool, bool]:
    """Sort, match and save one e-mail, moving its card first; whether it moved a card and whether it is to check.
    Mail that stays "other" leaves nothing behind: no row, no effect on any card."""
    name, domain = classify.sender(message["from"])
    label = classify.classify(domain, message["subject"], message["snippet"])
    thread = conn.execute(THREAD, (message["thread_id"],)).fetchone()
    found = match.match(cards, name, domain, message["subject"], thread[0] if thread else None)
    job, moved = (found[0] if len(found) == 1 else None), False
    job_like = (
        classify.job_system(domain) or thread is not None or classify.job_like(message["subject"], message["snippet"])
    )
    if label == "other" and job is not None and job_like:
        label = "unclear"  # a company you applied to wrote about the job and no phrase fits: the local model decides
    if label == "unclear" and job is None and not job_like:
        label = "other"  # e.g. a shop's "unfortunately your order is late": not about a job
    if label == "confirmation" and job is not None and tracker.stage(conn, job) not in (None, "ready"):
        label = "unclear"  # "thank you for applying" on a card already past it often opens a no: the model reads on
    if label == "other":
        return False, False
    check = label == "unclear" if job is not None else label != "other"  # unclear stays on the list until decided
    if job is not None:
        moved = apply.update(conn, job, label, datetime.fromisoformat(message["received_on"]))
    with conn:  # saved after its card: a crash in between only reads it again, and a card never moves back
        conn.execute(SAVE, (
            message["id"], message["thread_id"], message["received_on"], domain, label, job, int(moved), int(check),
            message["subject"] if check else None, json.dumps(found if check else []),
        ))  # fmt: skip
    return moved, check


def _sort_unclear(conn: sqlcipher3.Connection, gmail: Gmail, role: AIRole, scoring: Callable[[], bool]) -> int:
    """The local model labels the unclear e-mails still on your list; how many it sorted. Its text is read for this
    and never saved. One with its card moves that card and leaves the list; one without stays for you to choose the
    card, unless it is "other". It stops when scoring takes the graphics card or the model can't answer at all."""
    done = 0
    for message_id, subject, job, received_on in conn.execute(UNCLEAR, (MODEL_BATCH,)).fetchall():
        if scoring():
            break
        try:
            found = model.label(role, subject or "", gmail.message(message_id)["snippet"], gmail.body(message_id))
        except Gone:  # deleted in Gmail: it stays on your list
            continue
        except model.Unavailable:
            break
        if found is None:  # no answer now: it stays unclear, the next pass asks again
            continue
        moved = job is not None and apply.update(conn, job, found, datetime.fromisoformat(received_on))
        with conn:
            if job is not None or found == "other":
                conn.execute(SORTED_OFF, (found, int(moved), message_id))
            else:
                conn.execute(SORTED, (found, message_id))
        done += 1
    return done


def _count(number: int, word: str) -> str:
    return f"{number} {word}{'' if number == 1 else 's'}"
