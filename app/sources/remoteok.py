"""Remote OK's public feed: its newest jobs in one file. Its terms ask for a link back naming it (on the job page)."""

import html
from typing import Any
from urllib.parse import urlsplit

from app.sources import Job, SourceError, day_from_iso, day_from_timestamp, html_to_text, parse_each, required_text

LABEL = "Remote OK"
SOURCE = "feed:remoteok"
HOST = "remoteok.com"
API = f"https://{HOST}/api"
MIN_YEARLY = 10_000  # some listings put hourly pay in the same fields, which must not read as a year's


def read(fetcher: Any, roles: Any) -> tuple[list[Job], int]:
    """The feed's jobs and how many broken entries were skipped; it is one file, so `roles` narrow nothing here."""
    return parse(fetcher.get_json(API)[0])


def parse(data: Any) -> tuple[list[Job], int]:
    """Refused unless the first entry is the terms notice: without it, the terms may have changed."""
    if not isinstance(data, list) or not data or not isinstance(data[0], dict) or "legal" not in data[0]:
        raise SourceError("its first entry is not the terms notice (its terms or format may have changed)")
    jobs, skipped = parse_each(data[1:], _job)
    if not jobs:  # an empty or reshaped feed must never close every job
        raise SourceError("no usable jobs in the feed (its format may have changed)")
    return jobs, skipped


def _job(entry: dict) -> Job:
    link, place, text = _link(entry), entry.get("location"), entry.get("description")
    place = html.unescape(place).strip() if isinstance(place, str) else ""
    return Job(
        source=SOURCE,
        source_job_id=link,  # stays with the job when a board's link replaces this one
        company=html.unescape(required_text(entry, "company")),
        title=html.unescape(required_text(entry, "position")),
        url=link,
        locations=[place] if place else [],
        remote="remote",
        description=(html_to_text(html.unescape(text)) or None) if isinstance(text, str) else None,  # some come escaped
        salary=_salary(entry),
        date_posted=day_from_iso(entry.get("date")) or day_from_timestamp(entry.get("epoch")),
    )


def _link(entry: dict) -> str:
    """The listing's own address, written one way; anything but https on the feed's own site is a broken entry."""
    parts = urlsplit(required_text(entry, "url"))
    if parts.scheme != "https" or parts.netloc.lower() != HOST or not parts.path.strip("/"):
        raise ValueError("not a Remote OK listing")
    return f"https://{HOST}{parts.path}"


def _salary(entry: dict) -> str | None:
    low, high = entry.get("salary_min"), entry.get("salary_max")
    if not all(type(end) in (int, float) and end >= MIN_YEARLY for end in (low, high)) or low > high:
        return None
    return f"USD {low:,.0f}–{high:,.0f} per year"
