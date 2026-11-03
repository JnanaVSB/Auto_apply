"""Which jobs are ready for preparation, in one place for every page and automatic preparation (Phase 4c): a job
you marked "Prepare anyway", or one scored at or above the ready threshold by the chosen model with your current
resume on the ad as it is now. Money is only ever spent on jobs judged against your current resume."""

import sqlcipher3

# The SQL expects `jobs`, its quick check `a`, its score `s` from the chosen model, and `chosen (model, threshold,
# resume)`. A high score made with an older resume, or before the ad changed, waits to be scored again.
READY = "jobs.prepare_anyway OR (s.score >= chosen.threshold AND s.resume = chosen.resume AND NOT s.changed)"
STATUS = f"""CASE WHEN {READY} THEN 'ready' WHEN s.score >= chosen.threshold THEN 'waiting'
    WHEN s.score IS NOT NULL THEN 'low' WHEN s.failed IS NOT NULL THEN 'failed' WHEN a.passed = 0 THEN 'quick'
    ELSE 'waiting' END"""
# A job that can be prepared: still open and not skipped. Skip keeps the Prepare date, so Undo puts it back as it was.
PREPARABLE = "status = 'open' AND not_interested = 0"
QUEUE = (
    "UPDATE jobs SET prepare_requested_on = ? WHERE id = ? AND prepare_requested_on IS NULL"  # noqa: S608 (fixed text)
    f" AND {PREPARABLE}"
)


def queue(conn: sqlcipher3.Connection, job_id: int, now: str) -> bool:
    """You pressed Prepare: the job's Prepare date is kept (its box shows it). Pressing it again changes nothing."""
    with conn:
        return conn.execute(QUEUE, (now, job_id)).rowcount == 1


NEWEST = f"""WITH chosen (model, threshold, resume) AS (SELECT ?, ?, ?)
    SELECT jobs.id FROM jobs CROSS JOIN chosen LEFT JOIN ai_scores s ON s.job_id = jobs.id AND s.model = chosen.model
    WHERE ({READY}) AND {PREPARABLE}
    ORDER BY coalesce(jobs.date_posted, substr(jobs.first_seen, 1, 10)) DESC, jobs.id DESC LIMIT ?"""  # noqa: S608


def newest(conn: sqlcipher3.Connection, model: str, threshold: int, resume: str | None, limit: int) -> list[int]:
    """The ready jobs, newest first (by the date posted, else the day the app found them): automatic preparation's
    order (Phase 4c)."""
    return [job_id for (job_id,) in conn.execute(NEWEST, (model, threshold, resume, limit))]
