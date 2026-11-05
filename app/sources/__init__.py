"""The job shape every source produces, plus what sources share: board links, dates and HTML to plain text."""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from app.resume import CONTROL

# Job-board websites whose jobs can be read through a public API. European-hosted boards have no such API.
BOARD_HOSTS = {
    "boards.greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse",
    "jobs.lever.co": "lever",
    "jobs.ashbyhq.com": "ashby",
}
BOARD_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,99}")  # also keeps the name safe to put into an API address
JOB_ID = re.compile(r"[A-Za-z0-9-]+")
SAFE_HOST = re.compile(r"[a-z0-9._:-]*")  # what a host can hold once read; a browser refuses anything else
# LinkedIn, Handshake, and Indeed with its country sites (indeed.co.uk, indeed.com.au, ...), with any subdomain
CLOSED_SITE = re.compile(
    r"(.+\.)?(linkedin\.(com|cn)|lnkd\.in|joinhandshake\.(com|co\.uk)|indeed\.(com|[a-z]{2}|co\.[a-z]{2}|com\.[a-z]{2}))"
)
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
    # Each place to judge for "is it in the US", with the board's own country for it when given; empty = locations
    place_checks: list[tuple[str, str | None]] = field(default_factory=list)
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
    if skipped and not jobs:  # entries arrived but none could be read: never treat that as "no openings"
        raise SourceError(f"none of its {skipped:,} entries could be read (its data layout may have changed)")
    return jobs, skipped


def required_text(entry: dict, key: str) -> str:
    value = entry[key]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is empty")
    return value.strip()


def workplace(value: Any) -> str | None:
    """remote / hybrid / on-site from a board's own wording (OnSite, On-Site, onsite, Remote...)."""
    return WORKPLACES.get(re.sub(r"[^a-z]", "", value.lower())) if isinstance(value, str) else None


def host_of(url: str) -> str | None:
    """The site an address reaches, read as a browser reads it ("" when it names none, None when it can't be read):
    a backslash is a slash, and percent signs, full-width letters and other full stops still spell the same name."""
    try:
        host = urlsplit(url.strip().replace("\\", "/")).hostname or ""
        host = unquote(host).encode("idna").decode().rstrip(".").lower()  # "simplify.jobs." reaches the same site
        return host if SAFE_HOST.fullmatch(host) else None
    except (ValueError, UnicodeError):
        return None


def is_simplify_site(url: str) -> bool:
    """True for any address on simplify.jobs, which the app must never open itself (its terms forbid it), and for one
    that can't be read."""
    host = host_of(url)
    return host is None or host == "simplify.jobs" or host.endswith(".simplify.jobs")


def closed_site(url: str) -> bool:
    """True for a site the app never reads: you open it yourself. An address that can't be read counts as one."""
    host = host_of(url)
    return not host or CLOSED_SITE.fullmatch(host) is not None


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


def board_of(url: str) -> str | None:
    """The board (e.g. lever:acme) any address on a job-board site belongs to: a job, a form or the board itself."""
    parts = urlsplit(url.strip())
    kind = BOARD_HOSTS.get(parts.hostname or "")
    segments = [segment for segment in parts.path.split("/") if segment]
    if kind is None or not segments:
        return None
    board = parse_qs(parts.query).get("for", [""])[0] if segments[0] == "embed" else segments[0]  # Greenhouse's form
    board = board.lower()  # board names ignore capital letters
    return f"{kind}:{board}" if BOARD_NAME.fullmatch(board) else None


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
    BLOCKS = frozenset({"p", "div", "br", "li", "ul", "ol", "tr", "section", "h1", "h2", "h3", "h4", "h5", "h6"})
    HIDDEN = frozenset({"script", "style"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.held: list[str] = []  # text inside a script/style; kept only if that tag is never closed
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self.HIDDEN:
            self.hidden += 1
            self.held.clear()
        elif tag in self.BLOCKS:
            self.parts.append("\n- " if tag == "li" else "\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.HIDDEN:
            self.hidden = max(0, self.hidden - 1)
            self.held.clear()
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        (self.held if self.hidden else self.parts).append(data)


def _visible(html: str) -> str:
    parser = _TextOnly()
    parser.feed(html)
    parser.close()
    text = "".join(parser.parts)
    if parser.hidden:  # an unclosed <script> or <style> must not swallow the rest: read what followed as HTML
        text += "\n" + _visible("".join(parser.held))
    return text


def _tidy(text: str) -> str:
    lines = (" ".join(line.split()) for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def html_to_text(html: str) -> str:
    """The visible text of an HTML fragment; scripts, styles and tags are dropped, so nothing can run later."""
    return _tidy(_visible(html.replace("\0", "")))


def plain_text(text: str) -> str:
    """Text you pasted, as typed: nothing is read as HTML ("under <5 years" stays), and the pages show it escaped."""
    return _tidy(CONTROL.sub("", text))
