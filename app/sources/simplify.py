"""Simplify's public job lists on GitHub. The simplify.jobs website itself is never read."""

from typing import Any
from urllib.parse import urlsplit

from app.sources import Job, SourceError, day_from_timestamp, parse_each, posting_of, required_text

SPONSORSHIP = {
    "Offers Sponsorship": "offers",
    "Does Not Offer Sponsorship": "no",
    "U.S. Citizenship is Required": "citizens only",
}


def source_name(list_url: str) -> str:
    """The list's name in summaries and the database, e.g. simplify:Summer2027-Internships."""
    return "simplify:" + urlsplit(list_url).path.split("/")[2]


def parse(data: Any, source: str, level: str) -> tuple[list[Job], int]:
    """Turn one downloaded Simplify list into jobs; also return how many broken entries were skipped."""
    jobs, skipped = parse_each(data, lambda entry: _job(entry, source, level))
    if not jobs:  # an empty or reshaped list must never close every job
        raise SourceError("no usable jobs in the list (its format may have changed)")
    return jobs, skipped


def linked_boards(jobs: list[Job]) -> set[str]:
    """Boards (e.g. greenhouse:acme) that the list's active jobs link to."""
    postings = (posting_of(job.url) for job in jobs if job.listed)
    return {f"{posting[0]}:{posting[1]}" for posting in postings if posting}


def _job(entry: dict, source: str, level: str) -> Job | None:
    if entry.get("is_visible") is False:
        return None
    locations, terms, category = entry.get("locations") or [], entry.get("terms") or [], entry.get("category")
    if not isinstance(locations, list) or not isinstance(terms, list):
        raise ValueError("locations and terms must be lists")
    return Job(
        source=source,
        source_job_id=required_text(entry, "id"),
        company=required_text(entry, "company_name"),
        title=required_text(entry, "title"),
        url=required_text(entry, "url"),
        locations=[str(place) for place in locations],
        level=level,
        category=category if isinstance(category, str) else None,
        sponsorship=SPONSORSHIP.get(entry.get("sponsorship"), "unknown"),
        terms=[str(term) for term in terms],
        date_posted=day_from_timestamp(entry.get("date_posted")),
        listed=entry.get("active") is True,
    )
