"""The Answer key page: your yes / maybe / no marks, jobs worth marking, and how well each AI model and each
quick-check threshold agrees with your marks, so that the model and the thresholds come from your own judgement."""

from typing import NamedTuple

import sqlcipher3

from app.config import ScoringSettings
from app.scoring.stage_a import MIN_SHARED_SKILLS, PASSES
from app.web.queries import Scoring

LABELS = ("yes", "maybe", "no")
SUGGESTIONS = 10  # jobs per band
BANDS = (  # by meaning match: from, below
    ("Strong matches (meaning 0.70 and up)", 0.70, 2.0),
    ("Borderline (meaning 0.55 to 0.70)", 0.55, 0.70),
    ("Weak matches (meaning under 0.55)", -1.0, 0.55),
)
AI_THRESHOLDS = (50, 60, 70, 80)
MEANING_THRESHOLDS = (0.45, 0.50, 0.55, 0.60, 0.65)
ROUTES = (
    "your keyword in the title",
    "meaning under 0.50",
    "meaning 0.50 to 0.60",
    "meaning 0.60 to 0.70",
    "meaning 0.70 and up",
)
MARKS = "SELECT label, count(*) FROM answer_key GROUP BY label"
MARKED_OPEN = """SELECT k.job_id FROM answer_key k JOIN jobs j ON j.id = k.job_id WHERE j.status = 'open'
    ORDER BY k.marked_on, k.job_id"""
SUGGEST = """SELECT j.id, j.title, j.company, a.meaning FROM jobs j JOIN stage_a a ON a.job_id = j.id
    WHERE j.status = 'open' AND j.not_interested = 0 AND NOT a.changed AND a.meaning >= ? AND a.meaning < ?
        AND j.id NOT IN (SELECT job_id FROM answer_key)
    ORDER BY j.date_posted IS NULL, j.date_posted DESC, j.id DESC LIMIT ?"""
# Scores made with your current resume, from every model, on the jobs you marked.
AI_AGREEMENT = f"""SELECT s.model, k.label, count(s.score), count(s.failed),
        {", ".join(f"coalesce(sum(s.score >= {threshold}), 0)" for threshold in AI_THRESHOLDS)}
    FROM answer_key k JOIN ai_scores s ON s.job_id = k.job_id
    WHERE NOT s.changed AND s.resume = ? GROUP BY s.model, k.label ORDER BY s.model"""  # noqa: S608 (fixed numbers)
QUICK_AGREEMENT = f"""SELECT k.label, count(*), coalesce(sum({PASSES}), 0)
    FROM answer_key k JOIN stage_a a ON a.job_id = k.job_id WHERE NOT a.changed GROUP BY k.label"""  # noqa: S608
# Open jobs that passed the quick check, by the way they passed, and how the chosen model scored them.
BY_ROUTE = """SELECT CASE WHEN a.keywords != '[]' THEN 0 WHEN a.meaning < 0.50 THEN 1 WHEN a.meaning < 0.60 THEN 2
        WHEN a.meaning < 0.70 THEN 3 ELSE 4 END AS route, count(*), count(s.score),
        coalesce(sum(s.score >= :threshold), 0)
    FROM jobs j JOIN stage_a a ON a.job_id = j.id
    LEFT JOIN ai_scores s ON s.job_id = j.id AND s.model = :model AND NOT s.changed AND s.resume = :resume
    WHERE j.status = 'open' AND a.passed AND NOT a.changed GROUP BY route ORDER BY route"""


class Counts(NamedTuple):
    scored: int
    failed: int
    passes: tuple[int, ...]  # how many scored at least each of AI_THRESHOLDS


class Route(NamedTuple):
    label: str
    passed: int
    scored: int
    ready: int


def marks(conn: sqlcipher3.Connection) -> dict[str, int]:
    return dict.fromkeys(LABELS, 0) | dict(conn.execute(MARKS).fetchall())


def marked_open(conn: sqlcipher3.Connection) -> list[int]:
    """The open jobs you marked: what "Score my marked jobs" scores."""
    return [job_id for (job_id,) in conn.execute(MARKED_OPEN)]


def suggestions(conn: sqlcipher3.Connection) -> dict[str, list[dict]]:
    """Open jobs you have not marked, newest first, from strong, borderline and weak meaning matches."""
    columns = ("id", "title", "company", "meaning")
    return {
        band: [dict(zip(columns, row, strict=True)) for row in conn.execute(SUGGEST, (low, high, SUGGESTIONS))]
        for band, low, high in BANDS
    }


def ai_agreement(conn: sqlcipher3.Connection, resume: str | None) -> dict[str, dict[str, Counts]]:
    """For each model, and each of your labels: how many of those jobs it scored, failed, and passed at each
    threshold. Only scores made with your current resume count; none without a usable resume."""
    found: dict[str, dict[str, Counts]] = {}
    for model, label, scored, failed, *passes in conn.execute(AI_AGREEMENT, (resume,)):
        found.setdefault(model, {})[label] = Counts(scored, failed, tuple(passes))
    return found


def quick_agreement(conn: sqlcipher3.Connection, settings: ScoringSettings) -> dict[float, dict[str, tuple[int, int]]]:
    """For each meaning threshold (with today's skills threshold): of your marked jobs with a quick check, per label,
    how many there are and how many would pass."""
    found = {}
    for meaning in sorted({*MEANING_THRESHOLDS, settings.meaning_threshold}):
        rule = {"min_shared": MIN_SHARED_SKILLS, "skills": settings.skills_threshold, "meaning": meaning}
        found[meaning] = {label: (checked, passed) for label, checked, passed in conn.execute(QUICK_AGREEMENT, rule)}
    return found


def pass_through(conn: sqlcipher3.Connection, scoring: Scoring) -> list[Route]:
    """How the open jobs that passed the quick check passed, and how many of them the chosen model then found ready."""
    values = {"model": scoring.model, "threshold": scoring.threshold, "resume": scoring.resume}
    return [Route(ROUTES[route], *numbers) for route, *numbers in conn.execute(BY_ROUTE, values)]
