"""Rules shared by every source (roles, US location, level, remote, links) and saving one source's jobs."""

import json
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from itertools import groupby
from operator import itemgetter
from typing import NamedTuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import sqlcipher3

from app import places
from app.sources import Job, is_simplify_site, posting_of

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
SELECT_FIELDS = f"SELECT {', '.join(FIELDS)}, added_by_you, check_location FROM jobs WHERE id = ?"  # noqa: S608 (fixed names)
INSERT_JOB = """INSERT INTO jobs (apply_url, match_key, first_seen, last_seen, status, open_manually_only,
    added_by_you, company, title, locations, us_location, remote, level, role_category, sponsorship, terms,
    description, salary, date_posted)
    VALUES (:apply_url, :match_key, :now, :now, 'open', :manual, :chosen, :company, :title, :locations, :us_location,
    :remote, :level, :role_category, :sponsorship, :terms, :description, :salary, :date_posted)"""
UPDATE_JOB = """UPDATE jobs SET last_seen = :now, company = :company, title = :title, locations = :locations,
    us_location = :us_location, remote = :remote, level = :level, role_category = :role_category,
    sponsorship = :sponsorship, terms = :terms, description = :description, salary = :salary,
    date_posted = :date_posted, added_by_you = :added, check_location = :check WHERE id = :id"""
# Who owns a job's details: the first Simplify entry / company-board entry that found it (see SIMPLIFY_OWNS),
# or, when the job has none of that kind, the first entry of any kind. Entries are job_sources rows.
OWNERS = """SELECT
    (SELECT min(rowid) FROM job_sources WHERE job_id = :id AND source LIKE 'simplify:%'),
    (SELECT min(rowid) FROM job_sources WHERE job_id = :id AND source NOT LIKE 'simplify:%'),
    (SELECT min(rowid) FROM job_sources WHERE job_id = :id),
    (SELECT rowid FROM job_sources WHERE source = :source AND source_job_id = :source_job_id)"""
# Why a source stopped listing a job; a job that is not open shows the reason of its deciding source.
REMOVED_FROM_BOARD, REMOVED_FROM_SIMPLIFY = "removed from its board", "removed from Simplify"
INACTIVE = "inactive on Simplify"  # only Simplify marks jobs inactive
BOARD_SHUT_DOWN, STOPPED_HIRING = "board shut down", "company stopped hiring"
NOT_RECORDED = "reason not recorded (before 2026-10-21)"  # filled in on the source's next successful read
SAVE_ENTRY = """INSERT INTO job_sources (source, source_job_id, job_id, listed, unlisted_on, unlisted_reason)
    VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (source, source_job_id) DO UPDATE SET job_id = excluded.job_id,
    listed = excluded.listed, unlisted_reason = excluded.unlisted_reason, unlisted_on = CASE
        WHEN excluded.listed THEN NULL WHEN job_sources.listed THEN excluded.unlisted_on
        ELSE job_sources.unlisted_on END"""  # the day it stopped being listed is kept on later runs
UNLIST = """UPDATE job_sources SET listed = 0, unlisted_reason = ?,
    unlisted_on = CASE WHEN listed THEN ? ELSE unlisted_on END WHERE source = ? AND source_job_id = ?"""
# Every source entry of the jobs `source` has. 'closed' is never set or changed here; it is left for a later phase.
ENTRIES = """SELECT j.id, j.status, j.status_reason, s.listed, s.unlisted_on, s.unlisted_reason,
        s.source NOT LIKE 'simplify:%', s.source NOT LIKE 'simplify:%' AND r.retired_on IS NULL
    FROM jobs j JOIN job_sources s ON s.job_id = j.id LEFT JOIN sources r ON r.name = s.source
    WHERE j.status != 'closed' AND j.id IN (SELECT job_id FROM job_sources WHERE source = ?) ORDER BY j.id"""


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
    unavailable: Counter = field(default_factory=Counter)  # became "no longer available", per reason
    skipped: int = 0  # unusable links


class _Entry(NamedTuple):
    """One source's state for a job."""

    listed: bool
    unlisted_on: str | None
    reason: str | None
    board: bool  # a company board (or a job you added), not a Simplify list
    live_board: bool  # ... that is still read (not retired)


def us_location(job: Job) -> str:
    """'yes', 'unclear' (kept for the user to decide) or 'no': every location is clearly outside the US."""
    verdicts = {_judge(place, country) for place, country in job.place_checks or [(p, None) for p in job.locations]}
    if "us" in verdicts:
        return "yes"
    return "no" if verdicts == {"foreign"} else "unclear"


def _judge(place: str, country: str | None) -> str:
    """One location: by its own country field when the board gives one, otherwise by its text."""
    if country and not places.REMOTE_ONLY.fullmatch(country):
        return "us" if places.US_WORDS.search(country) else "foreign"
    return places.classify(place)


def level(job: Job) -> str:
    if job.level:  # from the Simplify list or the board's own job type
        return job.level
    return next((name for name, pattern in LEVEL_PATTERNS if pattern.search(job.title)), "unknown")


def remote(job: Job) -> str:
    if job.remote:
        return job.remote
    text = " ".join(job.locations).lower()
    return "remote" if "remote" in text else "hybrid" if "hybrid" in text else "unknown"


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
    fragment = parts.fragment if parts.fragment.startswith(("/", "!/")) else ""  # "#/jobs/123" is the job's page
    host = parts.netloc.lower().removesuffix(".")  # "example.com." is the same site
    return urlunsplit(("https", host, parts.path.rstrip("/"), urlencode(query), fragment))


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
    reason: str | None = None,
) -> Counts:
    """Save one source's jobs all-or-nothing. When the list is `complete`, jobs it no longer lists are unlisted.

    `chosen` jobs were picked by the user, so the role and US filters do not apply to them. `reason` says why the
    missing jobs are gone; by default they were removed from that board or Simplify list.
    """
    counts = Counts()
    reason = reason or (REMOVED_FROM_SIMPLIFY if source.startswith("simplify:") else REMOVED_FROM_BOARD)
    with conn:  # one transaction: saved together at the end, or not at all
        for job in jobs:
            _save_one(conn, job, roles, now, counts, chosen)
        if complete:
            seen = {job.source_job_id for job in jobs}
            rows = conn.execute(
                "SELECT source_job_id FROM job_sources WHERE source = ? AND (listed = 1 OR unlisted_reason = ?)",
                (source, NOT_RECORDED),
            )
            gone = [(reason, now[:10], source, job_id) for (job_id,) in rows.fetchall() if job_id not in seen]
            conn.executemany(UNLIST, gone)
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
        "us_location": "unclear" if chosen and us == "no" else us,  # a job the user picked stays visible
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
        if not job.listed or (not chosen and (us == "no" or not roles.match(job))):  # foreign: never added
            return
        values = fields | {"apply_url": url, "match_key": match_key(job), "now": now, "chosen": chosen}
        values["manual"] = is_simplify_site(url)
        job_id = conn.execute(INSERT_JOB, values).lastrowid
    unlisted = (None, None) if job.listed else (now[:10], INACTIVE)
    # recorded before merging, so a newly found company board owns its details at once
    conn.execute(SAVE_ENTRY, (job.source, job.source_job_id, job_id, job.listed, *unlisted))
    counts.kept += job.listed
    if new:
        counts.new += 1
        return
    *row, old_added, old_check = conn.execute(SELECT_FIELDS, (job_id,)).fetchone()
    old = dict(zip(FIELDS, row, strict=True))
    owners = {"id": job_id, "source": job.source, "source_job_id": job.source_job_id}
    first_simplify, first_board, first, this_entry = conn.execute(OWNERS, owners).fetchone()

    def may_replace(name: str) -> bool:  # the owner's value wins; other entries only fill gaps
        return this_entry == ((first_simplify if name in SIMPLIFY_OWNS else first_board) or first)

    added, check = old_added or chosen, old_check
    if added:  # the user picked this job: its US label never changes
        fields["us_location"] = old["us_location"]
    elif fields["us_location"] == "no" and old["us_location"] != "no":  # a saved job is never hidden
        fields["us_location"] = "unclear"  # which never replaces a known label
        check = check or may_replace("us_location")  # its owner now says "not US": please check by hand

    merged = {
        name: value if value not in NOT_KNOWN and (old[name] in NOT_KNOWN or may_replace(name)) else old[name]
        for name, value in fields.items()
    }
    conn.execute(UPDATE_JOB, merged | {"now": now, "id": job_id, "added": added, "check": check})
    counts.updated += merged != old or (added, check) != (old_added, old_check)


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


def _status(entries: list[_Entry]) -> tuple[str, str | None]:
    """Open while a deciding source lists the job: its company boards that are still read, else all its sources.

    Otherwise the reason is that of the deciding source that stopped listing it last (a board first on the same day).
    """
    deciding = [entry for entry in entries if entry.live_board] or entries
    if any(entry.listed for entry in deciding):
        return "open", None
    latest = max(deciding, key=lambda entry: (entry.unlisted_on or "", entry.board))
    return "no longer available", latest.reason


def _refresh_status(conn: sqlcipher3.Connection, source: str, counts: Counts) -> None:
    rows = conn.execute(ENTRIES, (source,)).fetchall()
    for (job_id, old_status, old_reason), entries in groupby(rows, key=itemgetter(0, 1, 2)):
        status, reason = _status([_Entry(*row[3:]) for row in entries])
        if (status, reason) != (old_status, old_reason):
            conn.execute("UPDATE jobs SET status = ?, status_reason = ? WHERE id = ?", (status, reason, job_id))
            if old_status == "open":
                counts.unavailable[reason] += 1
            else:
                counts.updated += 1  # reopened, or its reason is now known
