"""Automatic preparation (off until you switch it on in Settings): within the council hours, ready jobs start
newest first, never more than the daily job cap, and only while a job's estimated full cost (app/council/estimate.py)
fits what is left of today's and this month's limits after what is spent, set aside and still to come for the jobs
already running. Only jobs never prepared, or stopped because your entries changed; your own presses always go."""

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import sqlcipher3

from app.config import OPENAI_HOST, REVIEWERS, AIRole, Config
from app.council import estimate, flow, runs, spending
from app.documents import writer_sources
from app.keychain import KeychainError, get_secret
from app.resume import ResumeError, read_resume
from app.scoring import ready

CANDIDATES = 200  # ready jobs looked at per pass, newest first
RECENT = "SELECT job_id, created_on FROM preparations WHERE kind != 'pilot' AND created_on >= ?"
RUNNING = "SELECT id, writer, council, mode FROM preparations WHERE status = 'writing'"
SPENT = "SELECT coalesce(sum(coalesce(cost, 0) + reserved), 0) FROM steps WHERE preparation_id = ?"


def start_due(conn: sqlcipher3.Connection, config: Config, now: datetime | None = None) -> str | None:
    """Starts the jobs automatic preparation may start now; returns why it waits (None when it is off, or started
    what it could)."""
    council, now = config.council, (now or datetime.now(UTC)).astimezone()
    if not council.automatic:
        return None
    if not council.any_time and not _in_hours(now.strftime("%H:%M"), council.start, council.end):
        return f"Automatic preparation waits for the council hours ({council.start} to {council.end})."
    roles = [config.ai.writer, *(getattr(config.ai, member) for member in REVIEWERS)]
    if missing := missing_key(roles):
        return f"Automatic preparation waits: no key is saved under '{missing}' (Settings)."
    try:
        resume = read_resume(config.data_dir).fingerprint
    except ResumeError:
        return "Automatic preparation waits: your resume can't be read."
    started = _started_today(conn, now)
    each = estimate.version(conn, config.ai.writer, dict(zip(REVIEWERS, roles[1:], strict=True)), council.rounds,
                            council.turns, half=True)  # fmt: skip
    found = spending.spent(conn, spending.today())
    coming = _coming(conn, config)
    room = min(config.spending.daily - found.today, config.spending.monthly - found.month) - coming
    offered = writer_sources.offered(conn, resume)
    for job_id in ready.newest(conn, config.ai.evaluator.model, config.scoring.ready_threshold, resume, CANDIDATES):
        latest = runs.latest(conn, job_id)
        current = runs.current(conn, job_id)
        if latest and not (latest["status"] == "stopped" and runs.outdated(latest, current)):
            continue  # prepared before: only your presses prepare it again
        if writer_sources.unconfirmed(offered, [chosen["source"] for chosen in current]):
            continue
        if started >= council.daily_jobs:
            return f"Automatic preparation waits: today's job cap of {council.daily_jobs} is reached."
        if each is None or each > room:
            return "Automatic preparation waits: a job's estimated cost doesn't fit what is left of your limits."
        flow.start(conn, config, job_id, automatic=True)
        started, room = started + 1, room - each
    return None


def _in_hours(time: str, start: str, end: str) -> bool:
    """Whether a time (HH:MM) is within the council hours, which may run past midnight."""
    return start <= time < end if start < end else time >= start or time < end


def missing_key(roles: list[AIRole]) -> str | None:
    """The name of a paid member's key that isn't saved (or can't be read), else None."""
    for role in roles:
        paid = role.service == "anthropic" or urlsplit(role.address).hostname == OPENAI_HOST
        if (paid or role.own) and role.key_name:
            try:
                if not get_secret(role.key_name):
                    return role.key_name
            except KeychainError:
                return role.key_name
    return None


def _started_today(conn: sqlcipher3.Connection, now: datetime) -> int:
    """How many jobs got a new version today (on this computer's clock): your presses count too."""
    since = (now - timedelta(days=2)).astimezone(UTC).isoformat(timespec="seconds")
    today = now.date()
    return len({job for job, created in conn.execute(RECENT, (since,))
                if datetime.fromisoformat(created).astimezone(now.tzinfo).date() == today})  # fmt: skip


def _coming(conn: sqlcipher3.Connection, config: Config) -> float:
    """What the versions still running may yet cost: each one's estimate less what it has spent so far."""
    total = 0.0
    for preparation, writer, council, mode in conn.execute(RUNNING).fetchall():
        members = {name: AIRole(**role) for name, role in json.loads(council or '{"members": {}}')["members"].items()}
        full = estimate.version(conn, AIRole(**json.loads(writer)), members, config.council.rounds,
                                config.council.turns, mode == "half")  # fmt: skip
        total += max((full or 0) - conn.execute(SPENT, (preparation,)).fetchone()[0], 0)
    return total
