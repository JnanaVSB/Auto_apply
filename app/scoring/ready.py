"""Which jobs are ready for preparation, in one place for every page and (from Phase 4c) the council's queue: a job
you marked "Prepare anyway", or one scored at or above the ready threshold by the chosen model with your current
resume on the ad as it is now. Money is only ever spent on jobs judged against your current resume."""

import sqlcipher3

# The SQL expects `jobs`, its quick check `a`, its score `s` from the chosen model, and `chosen (model, threshold,
# resume)`. A high score made with an older resume, or before the ad changed, waits to be scored again.
READY = "jobs.prepare_anyway OR (s.score >= chosen.threshold AND s.resume = chosen.resume AND NOT s.changed)"
STATUS = f"""CASE WHEN {READY} THEN 'ready' WHEN s.score >= chosen.threshold THEN 'waiting'
    WHEN s.score IS NOT NULL THEN 'low' WHEN s.failed IS NOT NULL THEN 'failed' WHEN a.passed = 0 THEN 'quick'
    ELSE 'waiting' END"""
QUEUE = "UPDATE jobs SET prepare_requested_on = ? WHERE id = ? AND status = 'open' AND prepare_requested_on IS NULL"


def queue(conn: sqlcipher3.Connection, job_id: int, now: str) -> bool:
    """You pressed Prepare: the job goes first in the Writer's queue. Pressing it again changes nothing."""
    with conn:
        return conn.execute(QUEUE, (now, job_id)).rowcount == 1
