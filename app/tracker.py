"""The Tracker (Phase 5): where each job with a finished resume version is on the board, Ready to Offer or Rejected,
and every move. A move made by hand is recorded as yours, so Phase 6 (Gmail) can leave it alone."""

from datetime import date, datetime, timedelta
from urllib.parse import urlsplit

import sqlcipher3

from app import collect, jobs
from app.fetch import Fetcher
from app.sources import posting_of

STAGES = {  # board order
    "ready": "Ready",
    "applied": "Applied",
    "assessment": "Online Assessment",
    "interview": "Interview",
    "offer": "Offer",
    "rejected": "Rejected",
}
ROUNDS = {"1": "Round 1", "2": "Round 2", "3": "Round 3", "4": "Round 4", "5": "Round 5", "final": "Final"}
WAITING = ("applied", "assessment", "interview")  # the stages whose cards go grey with no change and no e-mail
LANE_CARDS = 30  # cards a lane shows before "Show all"
BAD_DATE = "Choose the date you applied: not later than tomorrow, not before 2000."
STALE = "Already moved in another tab: the board shows where it is now."
# A job, its card (t) and its newest finished resume version (f), for the board and the job page alike.
CARDS = """FROM jobs j LEFT JOIN tracker t ON t.job_id = j.id
    LEFT JOIN (SELECT job_id, max(coalesce(finished_on, created_on)) AS done_on FROM preparations
        WHERE kind = 'resume' AND status = 'done' GROUP BY job_id) f ON f.job_id = j.id"""
# A card past Ready always shows, whatever "Not interested", closed or its versions; Ready holds a job with a finished
# resume version, or one you moved back, unless it is "Not interested".
SHOWN = "(t.stage != 'ready' OR (j.not_interested = 0 AND (t.job_id IS NOT NULL OR f.job_id IS NOT NULL)))"
BOARD = f"""SELECT j.id, j.company, j.title, j.apply_url, j.status, coalesce(t.stage, 'ready'), t.round, t.applied_on,
        t.changed_on, t.last_email_on, e.received_on, e.label {CARDS}
    LEFT JOIN emails e ON e.message_id = (SELECT message_id FROM emails WHERE job_id = j.id ORDER BY received_on DESC
        LIMIT 1)
    WHERE {SHOWN} ORDER BY coalesce(t.changed_on, f.done_on) DESC, j.id DESC"""  # noqa: S608 (fixed text)
PLACE = f"SELECT coalesce(t.stage, 'ready'), t.round, coalesce({SHOWN}, 0) {CARDS} WHERE j.id = ?"  # noqa: S608
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
# The job a link was saved as: its board posting (or the typed link, for another site), else a job saved earlier with
# that link. The saved address can differ from the typed one (the board's own link, or a job matched by its details).
FIND = """SELECT coalesce((SELECT job_id FROM job_sources WHERE source = ? AND source_job_id = ?),
    (SELECT id FROM jobs WHERE apply_url = ?))"""
ADD_MOVE = """INSERT INTO tracker_moves (job_id, from_stage, to_stage, round, moved_by, moved_on)
    VALUES (?, ?, ?, ?, ?, ?)"""


def move(conn: sqlcipher3.Connection, job_id: int, expected: str, to: str, on: date, moved_by: str = "you") -> bool:
    """Move the card from `expected` (the stage the page showed) to `to`, on the local date `on`, and record it.
    False when it was moved meanwhile (another tab). The caller checks the job exists and, from Ready, that it is on
    the board (Add an application skips this on purpose: it puts a new job straight in Applied)."""
    if expected not in STAGES or to not in STAGES or expected == to:
        raise ValueError("Choose two different stages.")
    round_ = "1" if to == "interview" else None
    applied = None if to == "ready" else on.isoformat()
    at = collect.now()
    with conn:
        values = {"job": job_id, "to": to, "round": round_, "applied": applied, "now": at, "expected": expected}
        if conn.execute(MOVE, values).rowcount != 1:
            return False
        conn.execute(ADD_MOVE, (job_id, expected, to, round_, moved_by, at))
    return True


def add_application(
    conn: sqlcipher3.Connection, fetcher: Fetcher, link: str, company: str, role: str, applied_on: date, today: date
) -> tuple[str, int]:
    """An application made outside the app: the job is saved (once per link) and put in Applied on `applied_on`.
    A card already past Ready is left where it is. Returns the notice and the job's id; ValueError for bad input."""
    company, role = company.strip(), role.strip()
    if len(company) > 200 or len(role) > 200:
        raise ValueError("Company and role are at most 200 characters each.")
    if not date(2000, 1, 1) <= applied_on <= today + timedelta(days=1):
        raise ValueError(BAD_DATE)
    link = link.strip()
    posting = posting_of(link)
    source = (f"{posting[0]}:{posting[1]}", posting[2]) if posting else ("manual", link)
    found = (*source, jobs.clean_url(link))
    job_id = conn.execute(FIND, found).fetchone()[0]
    if job_id is None:  # saved first; a job already saved needs no board, even if its posting is gone
        collect.add(conn, fetcher, link, details=(company, role))  # CollectError if it can't be saved
        job_id = conn.execute(FIND, found).fetchone()[0]
    now_in = stage(conn, job_id)
    if now_in not in (None, "ready"):
        return f"Already on the Tracker, in {STAGES[now_in]}; not changed.", job_id
    if not move(conn, job_id, "ready", "applied", applied_on):
        return STALE, job_id
    return "Added to Applied.", job_id


def set_round(conn: sqlcipher3.Connection, job_id: int, round_: str) -> bool:
    """Change an Interview card's round and record it; False when the card isn't in Interview or has that round."""
    if round_ not in ROUNDS:
        raise ValueError("Choose a round from the list.")
    at = collect.now()
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


def on_board(conn: sqlcipher3.Connection, job_id: int) -> bool:
    """Whether the job has a card: a place on the Tracker, or a finished resume version (then it is in Ready)."""
    return bool(
        conn.execute(
            "SELECT EXISTS (SELECT 1 FROM tracker WHERE job_id = :job) OR EXISTS (SELECT 1 FROM preparations "
            "WHERE job_id = :job AND kind = 'resume' AND status = 'done')",
            {"job": job_id},
        ).fetchone()[0]
    )


def place(conn: sqlcipher3.Connection, job_id: int) -> tuple[str, str | None, bool]:
    """Where the job's card is: its stage, its round, and whether the board shows it (the board's own rule)."""
    stage_, round_, shown = conn.execute(PLACE, (job_id,)).fetchone()
    return stage_, round_, bool(shown)


def stage(conn: sqlcipher3.Connection, job_id: int) -> str | None:
    """The card's stage, or None when it has no row (a job with a finished version is then in Ready)."""
    row = conn.execute("SELECT stage FROM tracker WHERE job_id = ?", (job_id,)).fetchone()
    return row[0] if row else None


def board(conn: sqlcipher3.Connection, now: datetime, stale_days: int, show_all: str | None = None) -> list[dict]:
    """The lanes in board order, each {"stage", "name", "full", "cards", "count", "more", "grey_hidden"}; a lane shows
    its newest LANE_CARDS cards and counts the grey cards that leaves out, or, when it is `show_all`, all of them with
    the grey ones first. `now` is this computer's local time, aware: the day counter counts local dates, and a
    waiting card goes grey after `stale_days` with no change and no e-mail."""
    today, cutoff = now.date(), now - timedelta(days=stale_days)
    lanes = {stage: [] for stage in STAGES}
    rows = conn.execute(BOARD)
    for job_id, company, title, url, status, stage_, round_, applied_on, changed_on, emailed, *latest in rows:
        lanes[stage_].append(
            {
                "id": job_id,
                "company": company,
                "title": title,
                "site": (urlsplit(url).hostname or "").removeprefix("www."),
                "closed": status != "open",
                "round": round_,
                "days": None if applied_on is None else max(0, (today - date.fromisoformat(applied_on)).days),
                "email": _email(*latest),
                "stale": stage_ in WAITING and datetime.fromisoformat(max(changed_on, emailed or changed_on)) < cutoff,
            }
        )
    shown_lanes = []
    for stage_, name in STAGES.items():
        cards = lanes[stage_]
        full = show_all == stage_  # the whole lane, its grey cards (the ones to check) first
        shown = sorted(cards, key=lambda card: not card["stale"]) if full else cards[:LANE_CARDS]
        shown_lanes.append(
            {
                "stage": stage_,
                "name": name,
                "full": full,
                "cards": shown,
                "count": len(cards),
                "more": len(cards) > len(shown),
                "grey_hidden": sum(card["stale"] for card in cards[len(shown) :]),  # so a grey card is never missed
            }
        )
    return shown_lanes


def _email(received_on: str | None, label: str | None) -> tuple[str, str] | None:
    """A card's newest e-mail: its local day ("3 Nov") and what it was, or None."""
    if received_on is None:
        return None
    day = datetime.fromisoformat(received_on).astimezone()
    return f"{day.day} {day:%b}", label
