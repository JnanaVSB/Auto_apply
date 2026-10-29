"""Stage A, the quick check on this computer for every open job: your keywords in its title, the skills it shares
with your resume, how close it is in meaning to your resume, and phrases aimed at AI screening tools."""

import csv
import hashlib
import json
import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

import sqlcipher3
from rapidfuzz import fuzz

from app.config import ScoringSettings
from app.resume import CONTROL, Resume
from app.scoring.files import FILES, SKILLS
from app.scoring.meaning import Meaning

VERSION = 1  # raise when the checks change, so every job is checked again
BATCH = 64  # jobs checked and saved together, all-or-nothing
PROGRESS_EVERY = 6_400
FUZZY_LENGTH = 8  # keywords at least this long still match with a small typo
FUZZY_SCORE = 92
LONGEST_SKILL = 6  # words; longer skill names are left out
MIN_SHARED_SKILLS = 1
NOT_WORD = re.compile(r"[^a-z0-9+#]+")  # anything but letters, digits, + and # (C++, C#) separates words
TRICKS = re.compile(
    r"(?:ignore|disregard) (?:all |any )?(?:the )?(?:previous|prior|above|earlier) instructions"
    r"|(?:note|message|instructions?) (?:to|for) (?:any |the )?(?:ai|automated|llm)"
    r"|(?:ai|automated) (?:screening|resume|applicant)"
    r"|screening tools?\b"
    r"|^\s*system\s*:"
    r"|(?:rate|score|rank)\w* [^.\n]{0,60}at least \d+"
    r"|score of 100\b"
    r'|"score"\s*:\s*\d',  # a ready-made answer for the AI to copy
    re.IGNORECASE | re.MULTILINE,
)
# Whether a checked job passes, from its saved numbers, so a new threshold re-decides every job at once.
PASSES = """keywords != '[]' OR (json_array_length(shared_skills) >= :min_shared
    AND json_array_length(shared_skills) >= :skills * job_skills AND meaning >= :meaning)"""
TO_CHECK = """SELECT j.id FROM jobs j LEFT JOIN stage_a a ON a.job_id = j.id
    WHERE j.status = 'open' AND (a.job_id IS NULL OR a.changed OR a.setup != ?)
    ORDER BY j.date_posted DESC, j.id DESC"""
JOBS_BY_ID = "SELECT id, title, description FROM jobs WHERE id IN (SELECT value FROM json_each(?))"
SAVE = """INSERT OR REPLACE INTO stage_a (job_id, setup, keywords, job_skills, shared_skills, meaning, title_only,
    trick, passed, checked_on) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)"""


class Check(NamedTuple):
    keywords: list[str]  # yours, found in the title
    job_skills: int
    shared_skills: list[str]  # the job's skills that your resume has
    meaning: float  # 0 to 1
    title_only: bool
    trick: str | None  # the first phrase aimed at AI tools, as the ad writes it


def words(text: str) -> str:
    """Lower-case words with single spaces: "Scikit-Learn" and "scikit learn" are the same."""
    return " ".join(NOT_WORD.split(text.lower())).strip()


def _json(names: list[str]) -> str:
    return json.dumps(names, ensure_ascii=False)


def _phrases(text: str, longest: int) -> set[str]:
    """Every run of 1 to `longest` words in already-cleaned text."""
    tokens = text.split()
    return {" ".join(tokens[i : i + n]) for n in range(1, longest + 1) for i in range(len(tokens) - n + 1)}


def skill_names(folder: Path, extra: Iterable[str]) -> list[str]:
    """The O*NET software names, then your extra skills."""
    with (folder / SKILLS.name).open(encoding="utf-8", newline="") as file:
        return [row["Workplace Example"] for row in csv.DictReader(file)] + list(extra)


def setup_fingerprint(resume: Resume, keywords: Iterable[str], extra_skills: Iterable[str]) -> str:
    """Everything a check depends on besides the job itself: when it changes, every job is checked again."""
    parts = [VERSION, resume.fingerprint, [file.sha256 for file in FILES]]
    parts += [sorted({words(keyword) for keyword in keywords}), sorted({words(skill) for skill in extra_skills})]
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


class Checker:
    """What every job is compared with, prepared once per run."""

    def __init__(self, keywords: Iterable[str], skills: Iterable[str], resume: str, meaning: Meaning) -> None:
        self._keywords = {form: keyword for keyword in keywords if (form := words(keyword))}
        self._skills: dict[str, str] = {}
        for name in skills:  # "R", "C" and "Go" would match ordinary text such as "R&D"; "C#" stays
            form = words(name)
            if (len(form) > 2 or "+" in form or "#" in form) and len(form.split()) <= LONGEST_SKILL:
                self._skills.setdefault(form, name)
        self._longest_keyword = max((len(form.split()) for form in self._keywords), default=0)
        self._meaning = meaning
        self._mine = self._skills_in(resume)
        self._resume = meaning.fingerprints([resume])[0]

    def check(self, jobs: list[tuple[str, str | None]]) -> list[Check]:
        """Check (title, description) pairs together, since the meaning model reads faster in groups."""
        texts = [CONTROL.sub("", f"{title}\n{description or ''}").strip() for title, description in jobs]
        closeness = self._meaning.fingerprints(texts) @ self._resume
        checks = []
        for (title, description), text, meaning in zip(jobs, texts, closeness, strict=True):
            skills, trick = self._skills_in(text), TRICKS.search(text)
            checks.append(
                Check(
                    self._keywords_in(CONTROL.sub("", title)),
                    len(skills),
                    sorted(skills & self._mine),
                    round(max(float(meaning), 0.0), 4),
                    not (description or "").strip(),
                    trick[0].strip()[:80] if trick else None,
                )
            )
        return checks

    def _keywords_in(self, title: str) -> list[str]:
        """Your keywords in a title: as whole words, plural too, or with a small typo when long."""
        title = words(title)
        found = _phrases(title, self._longest_keyword)
        return sorted(
            keyword
            for form, keyword in self._keywords.items()
            if form in found
            or f"{form}s" in found
            or (len(form) >= FUZZY_LENGTH and fuzz.partial_ratio(form, title) >= FUZZY_SCORE)
        )

    def _skills_in(self, text: str) -> set[str]:
        return {self._skills[phrase] for phrase in _phrases(words(text), LONGEST_SKILL) if phrase in self._skills}


def run(
    conn: sqlcipher3.Connection,
    checker: Checker,
    setup: str,
    settings: ScoringSettings,
    stop: Callable[[], None] = lambda: None,
    out: Callable[[str], None] = print,
    only: list[int] | None = None,
) -> int:
    """Check every open job without an up-to-date check (or just those of `only`), newest first; returns how many
    were checked.

    Saved BATCH jobs at a time, all-or-nothing, so an interrupted run loses at most one batch. `stop` is called
    between batches and raises to stop.
    """
    rule = {"min_shared": MIN_SHARED_SKILLS, "skills": settings.skills_threshold, "meaning": settings.meaning_threshold}
    with conn:  # the thresholds may have changed since the last run
        conn.execute(f"UPDATE stage_a SET passed = ({PASSES}) WHERE passed IS NOT ({PASSES})", rule)  # noqa: S608 (fixed SQL)
    ids = [job_id for (job_id,) in conn.execute(TO_CHECK, (setup,))]
    if only is not None:  # "Score this job" and the like: not a full pass over every job
        wanted = set(only)
        ids = [job_id for job_id in ids if job_id in wanted]
    if ids:
        out(f"Quick check: {len(ids):,} jobs to check, newest first...")
    for start in range(0, len(ids), BATCH):
        stop()
        chosen = json.dumps(ids[start : start + BATCH])
        rows = conn.execute(JOBS_BY_ID, (chosen,)).fetchall()
        checks = checker.check([(title, description) for _, title, description in rows])
        now = datetime.now(UTC).isoformat(timespec="seconds")
        results = [
            (job_id, setup, _json(check.keywords), check.job_skills, _json(check.shared_skills), check.meaning,
             check.title_only, check.trick, now)
            for (job_id, *_), check in zip(rows, checks, strict=True)
        ]  # fmt: skip
        with conn:
            conn.executemany(SAVE, results)
            conn.execute(
                f"UPDATE stage_a SET passed = ({PASSES}) WHERE job_id IN (SELECT value FROM json_each(:ids))",  # noqa: S608 (fixed SQL)
                rule | {"ids": chosen},
            )
        done = min(start + BATCH, len(ids))
        if done % PROGRESS_EVERY == 0 or done == len(ids):
            out(f"  {done:,} of {len(ids):,} checked")
    return len(ids)
