"""The job shape every source produces, plus what sources share: board links, dates and HTML to plain text."""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlsplit

# Job-board websites whose jobs can be read through a public API. European-hosted boards have no such API.
BOARD_HOSTS = {
    "boards.greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse",
    "jobs.lever.co": "lever",
    "jobs.ashbyhq.com": "ashby",
}
BOARD_NAME = re.compile(r"[a-z0-9][a-z0-9._-]*")  # also keeps the name safe to put into an API address
JOB_ID = re.compile(r"[A-Za-z0-9-]+")
WORKPLACES = {"remote": "remote", "hybrid": "hybrid", "onsite": "on-site"}


class SourceError(Exception):
    """A source's data is not in the expected shape; that source is skipped and closes nothing."""


@dataclass
class Job:
    """One job as a source sees it, before the shared rules in app/jobs.py."""

    source: str  # a Simplify list or a board, e.g. greenhouse:acme
    source_job_id: str
    company: str
    title: str
    url: str
    locations: list[str]
    countries: list[str] = field(default_factory=list)  # from the board's own country fields
    remote: str | None = None  # remote / hybrid / on-site, when the source says
    level: str | None = None  # intern / new grad, from the Simplify list
    category: str | None = None
    sponsorship: str = "unknown"
    terms: list[str] = field(default_factory=list)
    description: str | None = None  # plain text
    salary: str | None = None
    date_posted: str | None = None  # YYYY-MM-DD
    listed: bool = True  # False when the source marks the job inactive


def parse_each(entries: Any, to_job: Callable[[Any], Job | None]) -> tuple[list[Job], int]:
    """Turn each entry into a job; return the jobs and how many broken entries were skipped.

    `to_job` returns None for an entry to leave out on purpose (hidden, unlisted); errors mean a broken entry.
    """
    if not isinstance(entries, list):
        raise SourceError("unexpected format (no list of jobs)")
    jobs, skipped = [], 0
    for entry in entries:
        try:
            job = to_job(entry)
        except (AttributeError, KeyError, TypeError, ValueError):
            skipped += 1
            continue
        if job is not None:
            jobs.append(job)
    return jobs, skipped


def required_text(entry: dict, key: str) -> str:
    value = entry[key]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is empty")
    return value.strip()


def workplace(value: Any) -> str | None:
    """remote / hybrid / on-site from a board's own wording (OnSite, On-Site, onsite, Remote...)."""
    return WORKPLACES.get(re.sub(r"[^a-z]", "", value.lower())) if isinstance(value, str) else None


def posting_of(url: str) -> tuple[str, str, str] | None:
    """(kind, board, job id) for a Greenhouse, Lever or Ashby job link; None for any other link."""
    parts = urlsplit(url.strip())
    kind = BOARD_HOSTS.get(parts.hostname or "")
    if kind is None or parts.scheme not in ("https", "http"):
        return None
    segments = [segment for segment in parts.path.split("/") if segment]
    board = job_id = ""
    if kind == "greenhouse" and segments[:2] == ["embed", "job_app"]:
        query = parse_qs(parts.query)
        board, job_id = query.get("for", [""])[0], query.get("token", [""])[0]
    elif kind == "greenhouse":
        if len(segments) >= 3 and segments[1] == "jobs":
            board, job_id = segments[0], segments[2]
    elif len(segments) >= 2:
        board, job_id = segments[0], segments[1]
    board = board.lower()  # board names ignore capital letters
    if not (BOARD_NAME.fullmatch(board) and JOB_ID.fullmatch(job_id)):
        return None
    return kind, board, job_id


def day_from_timestamp(timestamp: Any) -> str | None:
    """YYYY-MM-DD from a Unix time in seconds or milliseconds; None when missing or impossible."""
    if not isinstance(timestamp, int | float) or timestamp <= 0:
        return None
    if timestamp > 10**11:  # no date in seconds is this large, so it is in milliseconds
        timestamp /= 1000
    try:
        return datetime.fromtimestamp(timestamp, UTC).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def day_from_iso(text: Any) -> str | None:
    """YYYY-MM-DD from a date-time like 2026-10-19T11:17:24-04:00; None when missing or invalid."""
    try:
        return datetime.fromisoformat(text).date().isoformat()
    except (TypeError, ValueError):
        return None


class _TextOnly(HTMLParser):
    BLOCKS = {"p", "div", "br", "li", "ul", "ol", "tr", "section", "h1", "h2", "h3", "h4", "h5", "h6"}
    HIDDEN = {"script", "style"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self.HIDDEN:
            self._hidden += 1
        elif tag in self.BLOCKS:
            self.parts.append("\n- " if tag == "li" else "\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.HIDDEN:
            self._hidden = max(0, self._hidden - 1)
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._hidden:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """The visible text of an HTML fragment; scripts, styles and tags are dropped, so nothing can run later."""
    parser = _TextOnly()
    parser.feed(html.replace("\0", ""))
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
