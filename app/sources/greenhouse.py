"""Greenhouse public job boards (boards-api.greenhouse.io), with full descriptions."""

import html
import re
from typing import Any

from app.sources import Job, day_from_iso, html_to_text, parse_each, required_text, workplace

BOARD_PAGE = "https://job-boards.greenhouse.io/{}"  # the board's public page, to check by hand
JOB_LINK = "https://job-boards.greenhouse.io/{}/jobs/{}"  # one standard link per job: board, then job id


def api_url(board: str) -> str:
    return f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"


def parse(data: Any, board: str) -> tuple[list[Job], int]:
    """Turn one board's reply into jobs; also return how many broken entries were skipped."""
    return parse_each(data.get("jobs") if isinstance(data, dict) else None, lambda entry: _job(entry, board))


def reported_company(jobs: list[Job], board: str) -> str | None:
    """The company name the board itself reports, from its jobs; None when they give none (a job without one is
    stored under the board's own name, which the board did not report)."""
    return next((job.company for job in jobs if job.company != board), None)


def _job(entry: dict, board: str) -> Job:
    if not isinstance(entry["id"], int | str):
        raise ValueError("id is missing")
    offices = [office.get("location") for office in entry.get("offices") or [] if isinstance(office, dict)]
    metadata = [item.get("value") for item in entry.get("metadata") or [] if isinstance(item, dict)]
    location = (entry.get("location") or {}).get("name") or ""
    locations = [place.strip() for place in re.split(r"[|;]", location) if place.strip()]
    company = entry.get("company_name")
    return Job(
        source=f"greenhouse:{board}",
        source_job_id=str(entry["id"]),
        company=company.strip() if isinstance(company, str) and company.strip() else board,
        title=required_text(entry, "title"),
        url=required_text(entry, "absolute_url"),
        locations=locations,
        place_checks=[(place, None) for place in [*locations, *offices] if isinstance(place, str) and place.strip()],
        remote=next(filter(None, map(workplace, metadata)), None),  # from the "Location Type" field
        description=html_to_text(html.unescape(entry.get("content") or "")) or None,  # sent HTML-escaped
        date_posted=day_from_iso(entry.get("first_published") or entry.get("updated_at")),
    )
