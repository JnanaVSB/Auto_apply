"""Remote OK's public feed: its newest jobs in one file. Its terms ask for a link back naming it (on the job page)."""

import html
from typing import Any
from urllib.parse import urlsplit

from app.sources import (
    Job,
    SourceError,
    day_from_iso,
    day_from_timestamp,
    html_to_text,
    parse_each,
    required_name,
    required_text,
    yearly_pay,
)

LABEL = "Remote OK"
SOURCE = "feed:remoteok"
HOST = "remoteok.com"
API = f"https://{HOST}/api"


def read(fetcher: Any, keywords: list[str]) -> tuple[list[Job], int, bool]:
    """The feed's jobs, how many broken entries were skipped, and that the list is whole: it is one file, so `keywords`
    narrow nothing here."""
    return *parse(fetcher.get_json(API)[0]), True


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
        company=required_name(entry, "company"),
        title=required_name(entry, "position"),
        url=link,
        locations=[place] if place else [],
        remote="remote",
        description=(html_to_text(html.unescape(text)) or None) if isinstance(text, str) else None,  # some come escaped
        salary=yearly_pay(entry.get("salary_min"), entry.get("salary_max")),
        date_posted=day_from_iso(entry.get("date")) or day_from_timestamp(entry.get("epoch")),
    )


def _link(entry: dict) -> str:
    """The listing's own address, written one way; anything but https on the feed's own site is a broken entry."""
    parts = urlsplit(required_text(entry, "url"))
    if parts.scheme != "https" or parts.netloc.lower() != HOST or not parts.path.strip("/"):
        raise ValueError("not a Remote OK listing")
    return f"https://{HOST}{parts.path}"
