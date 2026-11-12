"""The Summary page, the first page after sign-in: what needs your attention, counted from the database. Read-only;
every link only opens a page. Days are this computer's local dates; stored UTC times are compared with the UTC start
of each local day."""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

import sqlcipher3
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response

from app.scoring.ready import STATUS
from app.tracker import STAGES
from app.web import queries

DAYS = 91  # the longest chart: 13 columns of 7 days ending today
PERIODS = {7: (7, 1), 30: (30, 1), 90: (DAYS, 7)}  # the chart's switch: days shown, and days per column
REPLIES = "'assessment', 'interview', 'offer'"  # the stages a reply moves a card to
REPLY_LABELS = {**STAGES, "rejected": "Not moving on"}
# Each job's newest finished resume version (r), as the Tracker counts it
WRITTEN = """LEFT JOIN (SELECT job_id, max(coalesce(finished_on, created_on)) AS done_on FROM preparations
    WHERE kind = 'resume' AND status = 'done' GROUP BY job_id) r ON r.job_id = jobs.id"""
# Ready to review: a written job the Ready to Apply page shows, not yet applied for or recorded
READY = f"{queries.LISTABLE} AND {STATUS} = 'ready'"
REVIEW = f"{READY} AND r.done_on IS NOT NULL"
OPEN, SCORED = queries.PREPARABLE, f"{queries.PREPARABLE} AND s.score IS NOT NULL"
RECENT, THIS_WEEK = "jobs.first_seen >= d.month_at", "jobs.first_seen >= d.week_at"
# One pass over the jobs for the figures, their badges, the gauge and the pipeline; d holds the UTC starts of today,
# this week and the last 30 days, and the week's first local date for applied_on
COUNTS = (
    queries.SCORED.format(
        columns=f"""
    count(CASE WHEN {OPEN} THEN 1 END), count(CASE WHEN {OPEN} AND {THIS_WEEK} THEN 1 END),
    count(CASE WHEN {REVIEW} THEN 1 END),
    count(CASE WHEN {REVIEW} AND r.done_on >= d.week_at THEN 1 END),
    count(CASE WHEN {READY} AND r.done_on IS NULL THEN 1 END),
    count(t.applied_on), count(CASE WHEN t.applied_on >= d.week THEN 1 END),
    count(CASE WHEN t.stage IN ({REPLIES}) THEN 1 END),
    count(CASE WHEN t.stage IN ({REPLIES}) AND t.changed_on >= d.week_at THEN 1 END),
    count(CASE WHEN {SCORED} THEN 1 END), count(CASE WHEN {SCORED} AND s.score >= chosen.threshold THEN 1 END),
    count(CASE WHEN {SCORED} AND s.scored_on >= d.today_at THEN 1 END),
    count(CASE WHEN {SCORED} AND s.score >= chosen.threshold AND s.scored_on >= d.today_at THEN 1 END),
    count(CASE WHEN {OPEN} AND s.score IS NULL AND s.failed IS NULL AND coalesce(a.passed, 1) = 1 THEN 1 END),
    count(CASE WHEN {OPEN} AND s.failed IS NOT NULL THEN 1 END),
    count(CASE WHEN {RECENT} THEN 1 END), count(CASE WHEN {RECENT} AND s.score IS NOT NULL THEN 1 END),
    count(CASE WHEN {RECENT} AND r.done_on IS NOT NULL THEN 1 END), count(CASE WHEN {RECENT} AND {REVIEW} THEN 1 END),
    count(CASE WHEN {RECENT} AND t.applied_on IS NOT NULL THEN 1 END),
    count(CASE WHEN {RECENT} AND t.stage IN ({REPLIES}) THEN 1 END),
    count(CASE WHEN {RECENT} AND t.stage = 'rejected' THEN 1 END)"""
    )
    + f""" CROSS JOIN (SELECT ? AS today_at, ? AS week_at, ? AS month_at, ? AS week) d
    LEFT JOIN tracker t ON t.job_id = jobs.id {WRITTEN}"""
)
NAMES = (
    "open", "open_week", "ready", "ready_week", "unwritten", "applied", "applied_week", "replies", "replies_week",
    "scored", "passed", "scored_today", "passed_today", "waiting", "failed",
    "collected", "p_scored", "p_written", "p_ready", "p_applied", "p_replied", "p_rejected",
)  # fmt: skip
MIDDLE = f"""SELECT s.score FROM jobs JOIN ai_scores s ON s.job_id = jobs.id AND s.model = ?
    WHERE {SCORED} ORDER BY s.score LIMIT ? OFFSET ?"""  # noqa: S608 (fixed text)
FOUND = "SELECT date(first_seen, 'localtime') AS day, count(*) FROM jobs WHERE first_seen >= ? GROUP BY day"
SENT = "SELECT applied_on, count(*) FROM tracker WHERE applied_on >= ? GROUP BY applied_on"
TO_REVIEW = (
    queries.SCORED.format(columns="jobs.id, jobs.title, jobs.company, s.score")
    + f""" {WRITTEN}
    WHERE {REVIEW} ORDER BY r.done_on DESC, jobs.id DESC LIMIT 5"""
)
TICKS, BLOCKS = 50, 12  # the gauge's ticks along its half circle; the blocks in each pipeline stage's stack
SERIES = ("collected", "sent")  # the chart's two lines
PIPELINE = (("Collected", "collected"), ("Scored", "p_scored"), ("Written", "p_written"), ("Ready", "p_ready"),
            ("Applied", "p_applied"), ("Replied", "p_replied"), ("Rejected", "p_rejected"))  # fmt: skip
MOVED = f"""SELECT j.id, j.title, j.company, t.stage FROM tracker t JOIN jobs j ON j.id = t.job_id
    WHERE t.stage IN ({REPLIES}, 'rejected') AND t.changed_on >= ?
    ORDER BY t.changed_on DESC, t.job_id DESC LIMIT 5"""  # noqa: S608 (fixed text)


def add_routes(app: FastAPI, page: Callable[..., HTMLResponse], scoring: Callable[[], queries.Scoring]) -> None:
    @app.get("/summary")
    async def summary_page(request: Request) -> Response:
        return page(request, "summary.html", **gather(request.app.state.conn, scoring(), date.today()))


def gather(conn: sqlcipher3.Connection, scoring: queries.Scoring, today: date) -> dict:
    """Every number the Summary page shows, counted as of `today`."""
    week, month, first = (today - timedelta(days) for days in (6, 29, DAYS - 1))
    week_at = _midnight(week)
    values = [*scoring.values(), _midnight(today), week_at, _midnight(month), str(week)]
    found = dict(zip(NAMES, conn.execute(COUNTS, values).fetchone(), strict=True))
    found["median"] = _median(conn, scoring, found["scored"])
    found["gauges"] = {"bar": _gauge(found["passed"], found["scored"]),
                       "scored": _gauge(found["scored"], found["open"])}  # fmt: skip
    counts = [found[key] for _, key in PIPELINE]
    found["pipeline"] = list(zip((name for name, _ in PIPELINE), counts, _blocks(counts), strict=True))
    collected = dict(conn.execute(FOUND, (_midnight(first),)).fetchall())
    sent = dict(conn.execute(SENT, (str(first),)).fetchall())
    days = [today - timedelta(back) for back in range(DAYS - 1, -1, -1)]
    found["charts"] = {period: _chart(days[-shown:], step, collected, sent)
                       for period, (shown, step) in PERIODS.items()}  # fmt: skip
    found["to_review"] = conn.execute(TO_REVIEW, scoring.values()).fetchall()
    found["moved"] = [(*row[:3], REPLY_LABELS[row[3]], row[3] == "rejected") for row in conn.execute(MOVED, (week_at,))]
    found["gmail"] = bool((conn.execute("SELECT connected_on FROM gmail_sync").fetchone() or (None,))[0])
    found["threshold"], found["next"], found["blocks"] = scoring.threshold, _next_step(found), BLOCKS
    return found


def _midnight(day: date) -> str:
    """The start of a local day as the UTC text the app stores, so stored times compare as text."""
    return datetime(day.year, day.month, day.day).astimezone(UTC).isoformat(timespec="seconds")


def _median(conn: sqlcipher3.Connection, scoring: queries.Scoring, count: int) -> int | None:
    """The middle score of the open scored jobs; two middles are averaged and rounded half up."""
    if not count:
        return None
    middle = [score for (score,) in conn.execute(MIDDLE, (scoring.model, 2 - count % 2, (count - 1) // 2))]
    return (sum(middle) + 1) // 2 if len(middle) == 2 else middle[0]


def _chart(days: list[date], step: int, collected: dict, sent: dict) -> dict:
    """One period of the chart: a column per day or per 7 days, its totals and applications per 100 jobs found."""
    columns = []
    for start in range(0, len(days), step):
        span = days[start : start + step]
        columns.append({"start": span[0], "end": span[-1], "collected": sum(collected.get(str(day), 0) for day in span),
                        "sent": sum(sent.get(str(day), 0) for day in span)})  # fmt: skip
    total, applied = sum(column["collected"] for column in columns), sum(column["sent"] for column in columns)
    per_100 = round(applied * 100 / total) if total else None  # none when nothing was found
    return {"columns": columns, "collected": total, "sent": applied, "per_100": per_100, **_lines(columns)}


def _lines(columns: list[dict]) -> dict:
    """The chart drawn on a box one unit wide per column and 100 high, 0 at the top: each column's two heights, scaled
    to the higher series (at least 1, so an empty period lies flat on the floor), and the points of the two lines and
    of the band between them, at each column's middle."""
    top = max(1, *(column[key] for column in columns for key in SERIES))
    for column in columns:
        column.update({f"y_{key}": round(100 - column[key] * 92 / top, 2) for key in SERIES})
    points = {key: [f"{x + 0.5},{column[f'y_{key}']}" for x, column in enumerate(columns)] for key in SERIES}
    return {f"line_{key}": " ".join(points[key]) for key in SERIES} | {
        "band": " ".join(points["collected"] + points["sent"][::-1])
    }


def _gauge(part: int, whole: int) -> dict:
    """A gauge's whole percent and how far its lit ticks reach along the half circle (100 long), both rounded half up;
    the percent never overstates (100 only when all, 0 only when none), and there is no percent and nothing lit when
    the whole is 0."""
    if not whole:
        return {"percent": None, "reach": 0}
    percent = min(max((200 * part + whole) // (2 * whole), 1 if part else 0), 99 if part < whole else 100)
    return {"percent": percent, "reach": (2 * TICKS * part + whole) // (2 * whole) * (100 // TICKS)}


def _blocks(counts: list[int]) -> list[int]:
    """How many of each stage's blocks are lit: in proportion to the largest stage, rounded up so a stage with any job
    shows at least one; none for an empty stage, and none at all when every stage is empty."""
    largest = max(counts)
    return [-(-BLOCKS * n // largest) if n else 0 for n in counts]


def _next_step(found: dict) -> tuple[str, str]:
    """The one thing to do next, as a button's words and the page it opens; it never sends anything."""
    if found["ready"]:
        return f"Review {found['ready']} ready application{'s' * (found['ready'] != 1)}", "/ready"
    if found["unwritten"]:
        return f"Prepare {found['unwritten']} job{'s' * (found['unwritten'] != 1)} ready for preparation", "/ready"
    if found["waiting"]:
        return f"Score {found['waiting']} waiting job{'s' * (found['waiting'] != 1)}", "/collect#scoring"
    return "Collect new jobs", "/collect"
