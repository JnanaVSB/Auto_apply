"""The scoring queue: the quick check on every open job, then the AI score for the jobs that passed, newest first and
strongest match first, within the scoring time per day. It pauses while a collection runs and continues where it
stopped; every job's result is saved as soon as it is done."""

import json
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import NamedTuple

import sqlcipher3

from app import llm
from app.config import Config
from app.resume import Resume
from app.scoring import stage_a, stage_b

AGE_GROUPS = (7, 30, 90)  # days since posting: 0-7 first, then 8-30, then 31-90; older jobs only when asked for
COLLECTION_HOURS = 6  # an unfinished collection record older than this was left by a crash or a power cut
PAUSE_CHECK_SECONDS = 10
PROGRESS_EVERY = 25
POSTED = "coalesce(j.date_posted, substr(j.first_seen, 1, 10))"
# Open jobs that passed an up-to-date quick check and have no up-to-date score from this model: none yet, or one made
# before the ad changed or with an older resume. A failed score is tried again only then, never every day.
WAITING = f"""SELECT j.id FROM jobs j JOIN stage_a a ON a.job_id = j.id
    LEFT JOIN ai_scores s ON s.job_id = j.id AND s.model = :model
    WHERE j.status = 'open' AND a.passed AND NOT a.changed AND a.setup = :setup AND {POSTED} >= :oldest
        AND (s.job_id IS NULL OR s.changed OR s.resume != :resume)
    ORDER BY CASE WHEN {POSTED} >= :week THEN 0 WHEN {POSTED} >= :month THEN 1 ELSE 2 END, a.meaning DESC, j.id"""  # noqa: S608 (fixed SQL)
JOB = """SELECT j.title, j.description, a.keywords, a.job_skills, a.shared_skills, a.meaning
    FROM jobs j JOIN stage_a a ON a.job_id = j.id WHERE j.id = ? AND j.status = 'open'"""
RUNNING = "SELECT 1 FROM collect_runs WHERE finished_at IS NULL AND started_at > ?"
Output = Callable[[str], None]


class Tally(NamedTuple):
    scored: int = 0
    ready: int = 0
    low: int = 0
    failed: int = 0
    odd: int = 0
    seconds: float = 0.0


def today() -> str:
    """The date on this computer's clock: the scoring time per day starts again at local midnight."""
    return date.today().isoformat()


def waiting(conn: sqlcipher3.Connection, model: str, setup: str, resume: str) -> list[int]:
    """The jobs to score automatically, in order."""
    return [job_id for (job_id,) in conn.execute(WAITING, _ages(model, setup, resume))]


def has_work(conn: sqlcipher3.Connection, model: str, setup: str, resume: str) -> bool:
    """Whether a run would do anything: jobs to check quickly, or jobs waiting for the AI."""
    checks = conn.execute(f"{stage_a.TO_CHECK} LIMIT 1", (setup,)).fetchone()  # noqa: S608 (fixed SQL)
    return bool(checks or conn.execute(f"{WAITING} LIMIT 1", _ages(model, setup, resume)).fetchone())  # noqa: S608


def _ages(model: str, setup: str, resume: str) -> dict:
    day = date.fromisoformat(today())
    week, month, oldest = (str(day - timedelta(days=days)) for days in AGE_GROUPS)
    return {"model": model, "setup": setup, "resume": resume, "oldest": oldest, "week": week, "month": month}


def used_today(conn: sqlcipher3.Connection) -> float:
    row = conn.execute("SELECT seconds FROM scoring_days WHERE day = ?", (today(),)).fetchone()
    return row[0] if row else 0.0


def collection_running(conn: sqlcipher3.Connection) -> bool:
    """A collection's own record says it started and has not finished (unless the record is too old to be real)."""
    since = (datetime.now(UTC) - timedelta(hours=COLLECTION_HOURS)).isoformat(timespec="seconds")
    return conn.execute(RUNNING, (since,)).fetchone() is not None


def wait_for_collections(conn: sqlcipher3.Connection, stop: Callable[[], None], out: Output) -> None:
    """Collections go first: while one runs, wait; `stop` is called while waiting and raises to stop."""
    if not collection_running(conn):
        return
    out("Paused: a collection is running; scoring continues when it finishes.")
    while collection_running(conn):
        stop()
        time.sleep(PAUSE_CHECK_SECONDS)
    out("The collection has finished; scoring continues.")


def run(
    conn: sqlcipher3.Connection,
    config: Config,
    checker: stage_a.Checker,
    setup: str,
    resume: Resume,
    *,
    job_ids: list[int] | None = None,
    stop: Callable[[], None] = lambda: None,
    out: Output = print,
    ask: stage_b.Ask = llm.ask,
) -> Tally:
    """The quick check on every open job that needs it, then the AI score.

    Without `job_ids`, the waiting jobs in queue order, within the scoring time per day. With `job_ids` ("Score
    these"), just those jobs, in that order, of any age, without using up the day's time.
    """

    def pause_or_stop() -> None:
        stop()
        wait_for_collections(conn, stop, out)

    settings, role = config.scoring, config.ai.evaluator
    stage_a.run(conn, checker, setup, settings, pause_or_stop, out)
    ids = waiting(conn, role.model, setup, resume.fingerprint) if job_ids is None else job_ids
    limit = settings.minutes_per_day * 60
    tally = Tally()
    if ids:
        out(f"AI scoring with {role.model}: {len(ids):,} jobs waiting...")
    for number, job_id in enumerate(ids, 1):
        pause_or_stop()
        if job_ids is None and used_today(conn) >= limit:
            out(f"Today's scoring time ({_clock(limit)}) is used up; scoring continues tomorrow.")
            break
        row = conn.execute(JOB, (job_id,)).fetchone()
        if row is None:  # closed meanwhile, or never checked
            continue
        title, description, keywords, job_skills, shared, meaning = row
        quick = stage_a.Check(json.loads(keywords), job_skills, json.loads(shared), meaning, False, None)
        result = stage_b.score_job(role, resume.text, title, description, quick, settings, ask=ask)
        stage_b.save(conn, job_id, role.model, result, resume.fingerprint, today() if job_ids is None else None)
        tally = _count(tally, result, settings.ready_threshold)
        if number % PROGRESS_EVERY == 0:
            out(f"  {number:,} of {len(ids):,} scored")
    if ids:
        out(_summary(tally, settings.ready_threshold, len(ids), used_today(conn), limit))
    return tally


def _count(tally: Tally, result: stage_b.Score, ready_threshold: int) -> Tally:
    ready = result.score is not None and result.score >= ready_threshold
    return Tally(
        tally.scored + 1,
        tally.ready + ready,
        tally.low + (result.score is not None and not ready),
        tally.failed + (result.score is None),
        tally.odd + (result.odd is not None),
        tally.seconds + result.seconds,
    )


def _clock(seconds: float) -> str:
    return str(timedelta(seconds=round(seconds)))


def _summary(tally: Tally, ready_threshold: int, queued: int, used: float, limit: float) -> str:
    speed = f" Speed: about {tally.scored / tally.seconds * 3600:,.0f} jobs an hour." if tally.seconds else ""
    return (
        f"Scored {tally.scored:,} jobs: {tally.ready:,} ready ({ready_threshold} or more), {tally.low:,} low score, "
        f"{tally.failed:,} failed (check by hand), {tally.odd:,} look odd. Time used today: {_clock(used)} of "
        f"{_clock(limit)}. Still waiting: {queued - tally.scored:,} jobs.{speed}"
    )
