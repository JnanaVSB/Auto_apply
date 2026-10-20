"""Rules shared by every source (roles, US location, level, remote, links) and saving one source's jobs."""

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import sqlcipher3

from app.sources import Job, posting_of

STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}  # fmt: skip
US_TEXT = re.compile(
    r"\bunited states\b|\b(?:" + "|".join(STATES.values()) + r")\b"
    r"|(?-i:(?<![A-Za-z])(?:USA|U\.S\.A?|US|SF|NYC|LA|South SF|Bay Area)(?![A-Za-z]))"
    r"|(?-i:,\s*(?:" + "|".join(STATES) + r")(?![A-Za-z]))",
    re.IGNORECASE,
)
REMOTE_ONLY = re.compile(r"(?:remote|anywhere|worldwide|global|[\s,;:/()–-])+", re.IGNORECASE)
LEVELS = (  # first match wins, so "Associate Director" is staff+ and "Engineer II" is mid
    ("intern", r"intern|internship|co-?op"),
    ("new grad", r"new grad|new graduate|university graduate|early career"),
    ("staff+", r"staff|principal|distinguished|director|head of"),
    ("senior", r"senior|sr\.?|lead"),
    ("mid", r"(?-i:II|III)"),
    ("entry", r"junior|jr\.?|entry level|associate|engineer (?-i:I)"),
)
LEVEL_PATTERNS = [(level, re.compile(rf"(?<!\w)(?:{words})(?!\w)", re.IGNORECASE)) for level, words in LEVELS]
STANDARD_LINKS = {
    "greenhouse": "https://job-boards.greenhouse.io/{}/jobs/{}",
    "lever": "https://jobs.lever.co/{}/{}",
    "ashby": "https://jobs.ashbyhq.com/{}/{}",
}
TRACKING = {"ref", "src", "source", "gh_src", "lever-source", "lever-origin", "trk"}
NOT_KNOWN = (None, "", "[]", "unknown", "unclear")  # never replace a known value when merging
SIMPLIFY_OWNS = {"company", "level", "role_category", "sponsorship", "terms"}  # company boards own the rest
FIELDS = (
    "company", "title", "locations", "us_location", "remote", "level", "role_category", "sponsorship", "terms",
    "description", "salary", "date_posted",
)  # fmt: skip
SELECT_FIELDS = """SELECT company, title, locations, us_location, remote, level, role_category, sponsorship, terms,
    description, salary, date_posted FROM jobs WHERE id = ?"""
INSERT_JOB = """INSERT INTO jobs (apply_url, match_key, first_seen, last_seen, status, open_manually_only,
    company, title, locations, us_location, remote, level, role_category, sponsorship, terms, description, salary,
    date_posted)
    VALUES (:apply_url, :match_key, :now, :now, 'open', :manual, :company, :title, :locations, :us_location, :remote,
    :level, :role_category, :sponsorship, :terms, :description, :salary, :date_posted)"""
UPDATE_JOB = """UPDATE jobs SET last_seen = :now, company = :company, title = :title, locations = :locations,
    us_location = :us_location, remote = :remote, level = :level, role_category = :role_category,
    sponsorship = :sponsorship, terms = :terms, description = :description, salary = :salary,
    date_posted = :date_posted WHERE id = :id"""
# Who owns a job's details: the first Simplify entry / company-board entry that found it (see SIMPLIFY_OWNS),
# or, when the job has none of that kind, the first entry of any kind. Entries are job_sources rows.
OWNERS = """SELECT
    (SELECT min(rowid) FROM job_sources WHERE job_id = :id AND source LIKE 'simplify:%'),
    (SELECT min(rowid) FROM job_sources WHERE job_id = :id AND source NOT LIKE 'simplify:%'),
    (SELECT min(rowid) FROM job_sources WHERE job_id = :id),
    (SELECT rowid FROM job_sources WHERE source = :source AND source_job_id = :source_job_id)"""
# A job is open while a deciding source lists it: its company board if it has one, otherwise Simplify.
STATUS_CHECK = """SELECT id, status, EXISTS (
    SELECT 1 FROM job_sources s WHERE s.job_id = jobs.id AND s.listed = 1 AND (s.source NOT LIKE 'simplify:%'
        OR NOT EXISTS (SELECT 1 FROM job_sources b WHERE b.job_id = jobs.id AND b.source NOT LIKE 'simplify:%')))
    FROM jobs WHERE id IN (SELECT job_id FROM job_sources WHERE source = ?)"""


@dataclass(frozen=True)
class Roles:
    """The user's target roles: Simplify categories, and keywords matched as whole words."""

    categories: frozenset[str]
    keywords: re.Pattern | None

    @classmethod
    def from_settings(cls, categories: Iterable[str], keywords: Iterable[str]) -> "Roles":
        words = [re.escape(word.strip()) for word in keywords if word.strip()]
        pattern = re.compile(rf"(?<!\w)(?:{'|'.join(words)})(?!\w)", re.IGNORECASE) if words else None
        return cls(frozenset(categories), pattern)

    def match(self, job: Job) -> bool:
        if job.category in self.categories:
            return True
        return self.keywords is not None and any(
            self.keywords.search(text or "") for text in (job.title, job.description)
        )


@dataclass
class Counts:
    kept: int = 0  # jobs from this source that are stored (new or already known)
    new: int = 0
    updated: int = 0
    closed: int = 0
    skipped: int = 0  # unusable links


def us_location(job: Job) -> str | None:
    """'yes', 'unclear' (kept for the user to decide) or None (not a US job)."""
    countries = [country for country in job.countries if not REMOTE_ONLY.fullmatch(country)]
    if countries:  # the board's own country field decides
        return "yes" if any(US_TEXT.search(country) for country in countries) else None
    if any(US_TEXT.search(place) for place in job.locations):
        return "yes"
    if not job.locations or any(REMOTE_ONLY.fullmatch(place) for place in job.locations):
        return "unclear"
    return None


def level(job: Job) -> str:
    if job.level:  # from the Simplify list or the board's own job type
        return job.level
    return next((name for name, pattern in LEVEL_PATTERNS if pattern.search(job.title)), "unknown")


def remote(job: Job) -> str:
    if job.remote:
        return job.remote
    places = " ".join(job.locations).lower()
    return "remote" if "remote" in places else "hybrid" if "hybrid" in places else "unknown"


def clean_link(job: Job) -> str | None:
    """One standard link per job, without tracking parts; None for anything but a web address."""
    kind, _, board = job.source.partition(":")
    posting = (kind, board, job.source_job_id) if kind in STANDARD_LINKS else posting_of(job.url)
    if posting:
        return STANDARD_LINKS[posting[0]].format(posting[1], posting[2])
    parts = urlsplit(job.url)
    if parts.scheme not in ("http", "https") or not parts.hostname or "@" in parts.netloc:
        return None
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING and not key.lower().startswith("utm_")
    ]
    return urlunsplit(("https", parts.netloc.lower(), parts.path.rstrip("/"), urlencode(query), ""))


def match_key(job: Job) -> str:
    """Company + title + locations, ignoring capitals, spaces and punctuation."""

    def squash(text: str) -> str:
        return "".join(char for char in text.lower() if char.isalnum())

    return "|".join((squash(job.company), squash(job.title), ",".join(sorted(map(squash, job.locations)))))


def save(
    conn: sqlcipher3.Connection,
    source: str,
    jobs: list[Job],
    roles: Roles,
    now: str,
    *,
    complete: bool = True,
    chosen: bool = False,
) -> Counts:
    """Save one source's jobs all-or-nothing. When the list is `complete`, jobs it no longer lists are unlisted.

    `chosen` jobs were picked by the user, so the role and US filters do not apply to them.
    """
    counts = Counts()
    with conn:  # one transaction: saved together at the end, or not at all
        for job in jobs:
            _save_one(conn, job, roles, now, counts, chosen)
        if complete:
            seen = {job.source_job_id for job in jobs}
            rows = conn.execute("SELECT source_job_id FROM job_sources WHERE source = ? AND listed = 1", (source,))
            gone = [(source, job_id) for (job_id,) in rows.fetchall() if job_id not in seen]
            conn.executemany("UPDATE job_sources SET listed = 0 WHERE source = ? AND source_job_id = ?", gone)
        _refresh_status(conn, source, counts)
    return counts


def _save_one(conn: sqlcipher3.Connection, job: Job, roles: Roles, now: str, counts: Counts, chosen: bool) -> None:
    url = clean_link(job)
    if url is None:
        counts.skipped += 1
        return
    job_id = _find(conn, job, url)
    us = us_location(job)
    fields = {
        "company": job.company,
        "title": job.title,
        "locations": json.dumps(job.locations, ensure_ascii=False),
        "us_location": us or "unclear",
        "remote": remote(job),
        "level": level(job),
        "role_category": job.category,
        "sponsorship": job.sponsorship,
        "terms": json.dumps(job.terms, ensure_ascii=False),
        "description": job.description,
        "salary": job.salary,
        "date_posted": job.date_posted,
    }
    new = job_id is None
    if new:
        if not job.listed or (not chosen and (us is None or not roles.match(job))):
            return
        host = urlsplit(url).hostname or ""
        manual = host == "simplify.jobs" or host.endswith(".simplify.jobs")  # the app must never open it itself
        values = fields | {"apply_url": url, "match_key": match_key(job), "now": now, "manual": manual}
        job_id = conn.execute(INSERT_JOB, values).lastrowid
    conn.execute(  # recorded before merging, so a newly found company board owns its details at once
        "INSERT INTO job_sources (source, source_job_id, job_id, listed) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (source, source_job_id) DO UPDATE SET job_id = excluded.job_id, listed = excluded.listed",
        (job.source, job.source_job_id, job_id, job.listed),
    )
    counts.kept += job.listed
    if new:
        counts.new += 1
        return
    old = dict(zip(FIELDS, conn.execute(SELECT_FIELDS, (job_id,)).fetchone(), strict=True))
    owners = {"id": job_id, "source": job.source, "source_job_id": job.source_job_id}
    first_simplify, first_board, first, this_entry = conn.execute(OWNERS, owners).fetchone()

    def may_replace(name: str) -> bool:  # the owner's value wins; other entries only fill gaps
        return this_entry == ((first_simplify if name in SIMPLIFY_OWNS else first_board) or first)

    merged = {
        name: value if value not in NOT_KNOWN and (old[name] in NOT_KNOWN or may_replace(name)) else old[name]
        for name, value in fields.items()
    }
    conn.execute(UPDATE_JOB, merged | {"now": now, "id": job_id})
    counts.updated += merged != old


def _find(conn: sqlcipher3.Connection, job: Job, url: str) -> int | None:
    """The stored copy of this job: same source id, else same link, else same company/title/places elsewhere."""
    for sql, values in (
        ("SELECT job_id FROM job_sources WHERE source = ? AND source_job_id = ?", (job.source, job.source_job_id)),
        ("SELECT id FROM jobs WHERE apply_url = ?", (url,)),
        (  # only another source's job: one source's separate postings with the same title stay separate
            "SELECT id FROM jobs WHERE match_key = ? AND id NOT IN (SELECT job_id FROM job_sources WHERE source = ?)",
            (match_key(job), job.source),
        ),
    ):
        row = conn.execute(sql, values).fetchone()
        if row:
            return row[0]
    return None


def _refresh_status(conn: sqlcipher3.Connection, source: str, counts: Counts) -> None:
    for job_id, old_status, is_open in conn.execute(STATUS_CHECK, (source,)).fetchall():
        status = "open" if is_open else "closed"
        if status != old_status:
            conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))
            counts.closed += status == "closed"
            counts.updated += status == "open"  # reopened
