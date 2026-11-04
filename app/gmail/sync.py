"""One Gmail sync pass: read the e-mails that are new since the last pass (each only once), sort and match them, move
their cards, and keep the ones it can't place for you to check; or the same for a period you choose. The caller holds
the lock and builds the client."""

import json
from collections.abc import Callable
from datetime import date, datetime, timedelta

import sqlcipher3

from app import tracker
from app.config import AIRole
from app.gmail import apply, classify, match, model
from app.gmail.client import MAX_PAGES, Gmail, GmailError, Gone, HistoryTooOld

# Gmail's own search only narrows what is read ({…} means "any of"); the rules decide what each e-mail is. Your own
# mail, drafts, spam, bin and chats are never searched; from the change list they are skipped by label (SKIPPED).
QUERY = "{since} -in:sent -in:drafts -in:spam -in:trash -in:chats"  # every e-mail ("Read all my e-mails")
JOB_SIGNS = (  # otherwise only these
    " {"
    + " ".join(f"from:{system}" for system in classify.JOB_SYSTEMS)
    + " subject:application subject:applying subject:interview subject:assessment subject:offer"
    " subject:unfortunately}"
)
SEARCH_LIMIT = MAX_PAGES * 100  # ids one search reads at most (100 a page)
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
# The model's answer is saved only if the e-mail still waits as unclear: a decision you made meanwhile wins
STILL = "label = 'unclear' AND to_check = 1 AND labelled_by != 'you'"
SORTED_OFF = f"""UPDATE emails SET label = ?, labelled_by = 'local model', moved = ?, to_check = 0, subject = NULL,
    candidates = '[]' WHERE message_id = ? AND {STILL}"""  # noqa: S608 (fixed text)
SORTED = f"UPDATE emails SET label = ?, labelled_by = 'local model' WHERE message_id = ? AND {STILL}"  # noqa: S608
WAITING = f"SELECT 1 FROM emails WHERE message_id = ? AND {STILL}"  # noqa: S608


def run(
    conn: sqlcipher3.Connection,
    gmail: Gmail,
    lookback_days: int,
    now: datetime,
    role: AIRole | None = None,
    scoring: Callable[[], bool] = lambda: False,
    read_all: bool = False,
) -> str:
    """One pass; returns its result in plain words (also saved; `failed` tells a failure's words apart). `read_all`
    ("Read all my e-mails") sorts every e-mail, not only those with a job sign. Then the local model (`role`, Ollama
    only) sorts the unclear e-mails, each only while no scoring run is using the graphics card. A GmailError is saved
    as the result and raised again; the e-mails done before it stay done, and the next pass starts from the last good
    one."""
    with conn:
        conn.execute("INSERT OR IGNORE INTO gmail_sync (id) VALUES (1)")
    history_id, last_sync = conn.execute("SELECT history_id, last_sync_on FROM gmail_sync WHERE id = 1").fetchone()
    try:
        ids, newest = _new_ids(gmail, history_id, last_sync, lookback_days, now, read_all)
        result = _said(*_read(conn, gmail, ids, role, scoring, read_all))
    except GmailError as error:
        with conn:
            conn.execute(RESULT, (str(error),))
        raise
    with conn:
        conn.execute(DONE, (newest, now.isoformat(timespec="seconds"), result))
    return result


def period(
    conn: sqlcipher3.Connection,
    gmail: Gmail,
    start: date,
    end: date,
    role: AIRole | None = None,
    scoring: Callable[[], bool] = lambda: False,
    read_all: bool = False,
) -> str:
    """The e-mails of a chosen period (both days included, in the account's time zone), sorted like a pass; its result
    in plain words. Nothing of the regular sync's is saved (its position, time and result); a GmailError is raised as
    it is. Gmail lists the newest first, so past the search limit the oldest wait for a period that ends earlier."""
    since = f"after:{start:%Y/%m/%d} before:{end + timedelta(days=1):%Y/%m/%d}"
    ids = gmail.search(QUERY.format(since=since) + ("" if read_all else JOB_SIGNS))
    result = _said(*_read(conn, gmail, ids, role, scoring, read_all), f" from {start} to {end}")
    if len(ids) >= SEARCH_LIMIT:
        result += (
            " That is the most one search reads: if the period has more, the oldest were not read; choose a period"
            " that ends earlier."
        )
    return result


def _read(
    conn: sqlcipher3.Connection,
    gmail: Gmail,
    ids: list[str],
    role: AIRole | None,
    scoring: Callable[[], bool],
    read_all: bool,
) -> tuple[int, int, int, int]:
    """Each e-mail of `ids` sorted, matched and saved (each only once), then the unclear ones the local model sorts;
    how many were read, moved a card, wait to check, and were sorted by the model."""
    read = moved = to_check = 0
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
        found = _one(conn, message, cards, read_all)
        read, moved, to_check = read + 1, moved + found[0], to_check + found[1]
    local = role is not None and role.service == "ollama"  # an e-mail's text never goes to a cloud model
    by_model = _sort_unclear(conn, gmail, role, scoring) if local else 0
    return read, moved, to_check, by_model


def _said(read: int, moved: int, to_check: int, by_model: int, where: str = "") -> str:
    said = f"Read {_count(read, 'new e-mail')}{where}: {_count(moved, 'card')} moved, {to_check} to check"
    return said + (f", {by_model} sorted by the local model." if by_model else ".")


def _new_ids(
    gmail: Gmail, history_id: str | None, last_sync: str | None, days: int, now: datetime, read_all: bool
) -> tuple[list, str]:
    """The ids to look at and the position to save. The position is taken before a search, so nothing that arrives
    during the search is missed (the next pass reads it from there)."""
    signs = "" if read_all else JOB_SIGNS
    if history_id:
        try:
            return gmail.history(history_id)
        except HistoryTooOld:  # too long without a sync: search again from the day before the last good one (Gmail
            # reads the date in the account's own time zone; e-mails already saved are skipped anyway)
            since = datetime.fromisoformat(last_sync) - timedelta(days=1) if last_sync else now - timedelta(days=days)
            newest = gmail.profile()[1]
            return gmail.search(QUERY.format(since=f"after:{since:%Y/%m/%d}") + signs), newest
    newest = gmail.profile()[1]
    return gmail.search(QUERY.format(since=f"newer_than:{days}d") + signs), newest


def _one(conn: sqlcipher3.Connection, message: dict, cards: list[dict], read_all: bool) -> tuple[bool, bool]:
    """Sort, match and save one e-mail, moving its card first; whether it moved a card and whether it is to check.
    Mail with no job signal (a job system, a thread already matched, or a job word) leaves nothing behind, whatever
    its phrases, unless `read_all` is on; so does job mail that stays "other" and matches no card."""
    name, domain = classify.sender(message["from"])
    thread = conn.execute(THREAD, (message["thread_id"],)).fetchone()
    known = classify.job_system(domain) or thread is not None
    signed = known or classify.job_like(message["subject"], message["snippet"])
    if not (signed or read_all):
        return False, False  # e.g. a pay slip's "take-home pay" or a shop's "unfortunately your parcel is late"
    label = classify.classify(domain, message["subject"], message["snippet"])
    if not signed and label != "other":
        label = "unclear"  # read all: mail with no job sign ("your availability for our webinar") never moves a card
    found = match.match(cards, name, domain, message["subject"], thread[0] if thread else None)
    if not (known or found or read_all or classify.job_like(message["subject"], "")):
        return False, False  # a stranger's mail about no card of yours is listed only when its subject is about a job
    job, moved = (found[0] if len(found) == 1 else None), False
    if label == "other" and found:
        label = "unclear"  # a company you applied to wrote about the job and no phrase fits: the model or you decide
    if label == "confirmation" and job is not None and tracker.stage(conn, job) not in (None, "ready", "applied"):
        label = "unclear"  # "thank you for applying" on a card already past Applied often opens a no
    if label == "other":
        return False, False
    check = job is None or label == "unclear"  # no card, or not sure: on the list until decided
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
        except Gone:  # deleted in Gmail: off your list, and never fetched again
            apply.dismiss(conn, message_id)
            continue
        except model.Unavailable:
            break
        if found is None or not conn.execute(WAITING, (message_id,)).fetchone():  # no answer, or you decided meanwhile
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


def failed(result: str | None) -> bool:
    """Whether a saved result is a failure's words rather than a pass's counts ("Read …")."""
    return bool(result) and not result.startswith("Read ")
