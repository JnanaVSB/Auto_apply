"""The scoring queue: the quick check on every open job, then the AI score for the jobs that passed, newest first and
strongest match first, within the scoring time per day. It pauses while a collection runs and continues where it
stopped; every job's result is saved as soon as it is done. A paid request is sent only within your spending limits."""

import json
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import NamedTuple

import sqlcipher3

from app import llm, questionnaire
from app.config import AIRole, Config
from app.council import spending
from app.documents import privacy, tile_sets
from app.resume import Resume, read_latex
from app.scoring import stage_a, stage_b

AGE_GROUPS = (7, 30, 90)  # days since posting: 0-7 first, then 8-30, then 31-90; older jobs only when asked for
COLLECTION_HOURS = 6  # an unfinished collection record older than this was left by a crash or a power cut
PAUSE_CHECK_SECONDS = 10
PROGRESS_EVERY = 25
SAME_FAILURES = 3  # jobs in a row failing the same way: the cause is Ollama or a setting, not the jobs
POSTED = "coalesce(j.date_posted, substr(j.first_seen, 1, 10))"
# Open jobs that passed an up-to-date quick check and have no up-to-date score from this model: none yet, or one made
# before the ad changed or with an older resume. A failed try is tried again only then, never every day.
WAITING = f"""SELECT j.id FROM jobs j JOIN stage_a a ON a.job_id = j.id
    LEFT JOIN ai_scores s ON s.job_id = j.id AND s.model = :model
    LEFT JOIN ai_failures f ON f.job_id = j.id AND f.model = :model AND NOT f.changed AND f.resume = :resume
    WHERE j.status = 'open' AND a.passed AND NOT a.changed AND a.setup = :setup AND {POSTED} >= :oldest
        AND (s.job_id IS NULL OR s.changed OR s.resume != :resume) AND f.job_id IS NULL
    ORDER BY CASE WHEN {POSTED} >= :week THEN 0 WHEN {POSTED} >= :month THEN 1 ELSE 2 END, a.meaning DESC, j.id"""  # noqa: S608 (fixed SQL)
JOB = """SELECT j.title, j.description, a.keywords, a.job_skills, a.shared_skills, a.meaning
    FROM jobs j JOIN stage_a a ON a.job_id = j.id WHERE j.id = ? AND j.status = 'open'"""
RUNNING = "SELECT 1 FROM collect_runs WHERE finished_at IS NULL AND started_at > ?"
USED_UP = """INSERT INTO scoring_days (day, seconds) VALUES (?, ?)
    ON CONFLICT (day) DO UPDATE SET seconds = max(seconds, excluded.seconds)"""
RESERVE = """INSERT INTO scoring_days (day, calls, reserved) VALUES (?, 1, ?)
    ON CONFLICT (day) DO UPDATE SET calls = calls + 1, reserved = reserved + excluded.reserved"""
CHARGED = """UPDATE scoring_days SET reserved = max(reserved - ?, 0), input_tokens = input_tokens + ?,
    cached_tokens = cached_tokens + ?, output_tokens = output_tokens + ?, cost = cost + ?, uncounted = max(uncounted, ?)
    WHERE day = ?"""
LEFT_OVER = "SELECT coalesce(sum(reserved), 0) FROM scoring_days"
KEPT = "UPDATE scoring_days SET cost = cost + reserved, reserved = 0 WHERE reserved > 0"
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
    call: Callable[..., llm.Reply] = llm.call,
) -> Tally:
    """The quick check on every open job that needs it, then the AI score.

    Without `job_ids`, the waiting jobs in queue order, within the scoring time per day. With `job_ids` ("Score
    these"), just those jobs, quick check included, in that order, of any age, without using up the day's time.
    A failed job is saved only once the next job goes differently. SAME_FAILURES in a row stop the run: the first is
    saved as failed, so that a repeat moves on, and the others stay waiting; the time of all of them counts.
    `call` sends one request (llm.call); a paid one only within your spending limits, its cost counted.
    """

    def pause_or_stop() -> None:
        stop()
        wait_for_collections(conn, stop, out)

    settings, role = config.scoring, config.ai.evaluator
    _left_over(conn, out)
    shield = None if role.service == "ollama" else _shield(conn, config)
    ask = _counted(conn, config, call) if shield is not None else lambda *found, **options: call(*found, **options).text
    stage_a.run(conn, checker, setup, settings, pause_or_stop, out, only=job_ids)
    ids = waiting(conn, role.model, setup, resume.fingerprint) if job_ids is None else job_ids
    daily = job_ids is None  # the queue's own run: its time counts towards the scoring time per day
    limit = settings.minutes_per_day * 60
    tally, held = Tally(), []  # held: failed jobs in a row, with the same reason
    if ids:
        out(f"AI scoring with {role.model}: {len(ids):,} jobs waiting...")
    for number, job_id in enumerate(ids, 1):
        pause_or_stop()
        if daily and used_today(conn) + _seconds(held) >= limit:  # held failures' time counts too
            out(f"Today's scoring time ({_clock(limit)}) is used up; scoring continues tomorrow.")
            break
        row = conn.execute(JOB, (job_id,)).fetchone()
        if row is None:  # closed meanwhile, or never checked
            continue
        title, description, keywords, job_skills, shared, meaning = row
        quick = stage_a.Check(json.loads(keywords), job_skills, json.loads(shared), meaning, False, None)
        result = stage_b.score_job(role, resume.text, title, description, quick, settings, ask=ask, shield=shield)
        if held and result.failed != held[-1][1].failed:
            _save(conn, held, role.model, resume.fingerprint, daily)
            held = []
        if result.failed:
            held.append((job_id, result))
            if len(held) == SAME_FAILURES:
                _save(conn, held[:1], role.model, resume.fingerprint, daily)
                if daily:
                    with conn:
                        conn.execute(stage_b.ADD_TIME, (today(), _seconds(held[1:])))
                reason = result.failed.removesuffix(stage_b.BY_HAND)
                raise llm.StopScoring(
                    f"{SAME_FAILURES} jobs in a row failed the same way: {reason}. The cause is most likely Ollama or "
                    "the AI model settings, not the jobs. The first job is saved as failed; the other "
                    f"{SAME_FAILURES - 1} stay not scored yet. Restart Ollama with `ollama serve` and try again."
                )
        else:
            _save(conn, [(job_id, result)], role.model, resume.fingerprint, daily)
        tally = _count(tally, result, settings.ready_threshold)
        if number % PROGRESS_EVERY == 0:
            out(f"  {number:,} of {len(ids):,} scored")
    _save(conn, held, role.model, resume.fingerprint, daily)
    if ids:
        out(_summary(tally, settings.ready_threshold, len(ids), used_today(conn), limit))
    return tally


def _counted(conn: sqlcipher3.Connection, config: Config, call: Callable[..., llm.Reply]) -> stage_b.Ask:
    """A paid request, as the council's: its largest possible cost set aside if it still fits your spending limits
    (else llm.StopScoring, and automatic scoring waits until tomorrow), then what it really cost instead."""

    def ask(role: AIRole, system: str, user: str, shape: dict, *, retry: bool) -> str:
        largest, day = spending.largest(role, system + user), spending.today()
        with conn:  # checked and set aside together, so the council can't take the same room
            if not (refusal := spending.refusal(conn, role, largest, config.spending, day)):
                conn.execute(RESERVE, (day, largest or 0))
        if refusal:
            with conn:
                conn.execute(USED_UP, (today(), config.scoring.minutes_per_day * 60))
            raise llm.StopScoring(refusal)
        usage, cost = llm.NOTHING, largest  # no answer, or a time-out: perhaps charged up to what was set aside
        try:
            reply = call(role, system, user, shape, retry=retry)
            usage, cost = reply.usage, spending.charged(role, reply.usage)
            return reply.text
        except llm.TimedOut:
            raise
        except llm.LLMError as error:  # refused or broken: what the service still charged for it
            usage, cost = error.usage, spending.charged(role, error.usage)
            raise
        finally:
            with conn:
                conn.execute(CHARGED, (largest or 0, *usage, cost or 0, cost is None, day))

    return ask


def _left_over(conn: sqlcipher3.Connection, out: Output) -> None:
    """What a run stopped while waiting for an answer (a crash, a power cut) had set aside stays counted: it may have
    been charged up to that. Only one scoring run sends at a time (score.SCORE_LOCK), so none is still waiting."""
    with conn:
        if left := conn.execute(LEFT_OVER).fetchone()[0]:
            conn.execute(KEPT)
    if left:
        out(f"The last scoring run stopped while waiting for an answer: up to ${left:.2f} is counted as spent.")


def _shield(conn: sqlcipher3.Connection, config: Config) -> privacy.Shield:
    """Before anything leaves this computer: the gate, then the privacy filter for your whole resume, built as the
    Writer's is (your Questionnaire's contact details, your resume's header, your confirmed entries' links)."""
    stage_b.gate(config.ai.evaluator, config.scoring)
    contact = questionnaire.load(conn)[0]["contact"]
    tiles = tile_sets.tiles(conn, tile_sets.latest_id(conn))  # none confirmed yet: no entries
    try:
        return privacy.shield(contact, [read_latex(config.data_dir)], tiles)
    except privacy.PrivacyError as error:  # no full name in the Questionnaire
        raise llm.StopScoring(str(error)) from None


def _save(
    conn: sqlcipher3.Connection, results: list[tuple[int, stage_b.Score]], model: str, resume: str, daily: bool
) -> None:
    """Each job's result; with `daily`, its time counts towards today's scoring time."""
    for job_id, result in results:
        stage_b.save(conn, job_id, model, result, resume, today() if daily else None)


def _seconds(results: list[tuple[int, stage_b.Score]]) -> float:
    return sum(result.seconds for _, result in results)


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
