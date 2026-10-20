"""Ashby public job boards (api.ashbyhq.com), with full descriptions and pay."""

from typing import Any

from app.sources import Job, SourceError, day_from_iso, html_to_text, parse_each, required_text, workplace


def api_url(board: str) -> str:
    return f"https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true"


def parse(data: Any, board: str) -> tuple[list[Job], int]:
    """Turn one board's reply into jobs; also return how many broken entries were skipped."""
    if not isinstance(data, dict):
        raise SourceError("unexpected format (no list of jobs)")
    return parse_each(data.get("jobs"), lambda entry: _job(entry, board))


def _job(entry: dict, board: str) -> Job | None:
    if entry.get("isListed") is False:
        return None
    secondary = [item for item in entry.get("secondaryLocations") or [] if isinstance(item, dict)]
    places = [entry.get("location"), *(item.get("location") for item in secondary)]
    addresses = [entry.get("address"), *(item.get("address") for item in secondary)]
    countries = [((address or {}).get("postalAddress") or {}).get("addressCountry") for address in addresses]
    pay = entry.get("compensation") or {}
    salary = pay.get("scrapeableCompensationSalarySummary") or pay.get("compensationTierSummary")
    return Job(
        source=f"ashby:{board}",
        source_job_id=required_text(entry, "id"),
        company=board,  # Ashby does not send the company name
        title=required_text(entry, "title"),
        url=required_text(entry, "jobUrl"),
        locations=[place.strip() for place in places if isinstance(place, str) and place.strip()],
        countries=[country for country in countries if isinstance(country, str)],
        remote=workplace(entry.get("workplaceType")) or ("remote" if entry.get("isRemote") is True else None),
        level="intern" if entry.get("employmentType") == "Intern" else None,
        description=html_to_text(entry.get("descriptionHtml") or "") or None,
        salary=salary if isinstance(salary, str) else None,
        date_posted=day_from_iso(entry.get("publishedAt")),
    )
