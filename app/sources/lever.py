"""Lever public job boards (api.lever.co), with full descriptions."""

import html
from typing import Any

from app.sources import Job, day_from_timestamp, html_to_text, parse_each, required_text, workplace


def api_url(board: str) -> str:
    return f"https://api.lever.co/v0/postings/{board}?mode=json"


def parse(data: Any, board: str) -> tuple[list[Job], int]:
    """Turn one board's reply into jobs; also return how many broken entries were skipped."""
    return parse_each(data, lambda entry: _job(entry, board))


def _job(entry: dict, board: str) -> Job:
    categories = entry.get("categories") or {}
    places = categories.get("allLocations") or [categories.get("location")]
    commitment, country = categories.get("commitment"), entry.get("country")
    sections = [entry.get("description") or ""]
    for item in entry.get("lists") or []:  # e.g. "What you'll do", with its bullet points
        if isinstance(item, dict):
            sections.append(f"<h3>{html.escape(str(item.get('text') or ''))}</h3><ul>{item.get('content') or ''}</ul>")
    sections.append(entry.get("additional") or "")
    return Job(
        source=f"lever:{board}",
        source_job_id=required_text(entry, "id"),
        company=board,  # Lever does not send the company name
        title=required_text(entry, "text"),
        url=required_text(entry, "hostedUrl"),
        locations=[place.strip() for place in places if isinstance(place, str) and place.strip()],
        countries=[country] if isinstance(country, str) else [],
        remote=workplace(entry.get("workplaceType")),
        level="intern" if isinstance(commitment, str) and "intern" in commitment.lower() else None,
        description=html_to_text("".join(sections)) or None,
        salary=_salary(entry.get("salaryRange")),
        date_posted=day_from_timestamp(entry.get("createdAt")),
    )


def _salary(pay: Any) -> str | None:
    """e.g. "USD 150,000–200,000 per year"."""
    if not isinstance(pay, dict) or not all(isinstance(pay.get(end), int | float) for end in ("min", "max")):
        return None
    interval = str(pay.get("interval") or "").removesuffix("-salary").replace("-", " ")
    return f"{pay.get('currency') or ''} {pay['min']:,.0f}–{pay['max']:,.0f} {interval}".strip()
