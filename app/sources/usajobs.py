"""USAJobs' public search, off until you switch it on and save your own key and e-mail (Settings → USAJobs key).

The key and e-mail go only in the request's headers. Details can change: the official announcement is what counts.
"""

import html
import re
from typing import Any
from urllib.parse import urlencode, urlsplit

from app.fetch import FetchError, Refused
from app.keychain import KeychainError, get_secret
from app.sources import (
    Job,
    Skipped,
    SourceError,
    day_from_iso,
    html_to_text,
    parse_each,
    required_name,
    required_text,
    yearly_pay,
)

LABEL = "USAJobs"
SOURCE = "feed:usajobs"
HOST = "www.usajobs.gov"  # where its listings are; the search itself is on data.usajobs.gov
API = "https://data.usajobs.gov/api/search"
KEY, EMAIL = "usajobs-key", "usajobs-email"
RESULTS_PER_PAGE, MAX_PAGES = 500, 5
NEEDS_KEY = "needs your key: Settings → USAJobs key"
NO_KEYWORDS = "no role keywords are set, so there is nothing to search for"
KEY_REFUSED = "refused the key: check it in Settings → USAJobs key"
CANT_SEND = "the saved key or e-mail can't be sent: save them again in Settings → USAJobs key"
SENDABLE = re.compile(r"[!-~]+")  # a request's headers carry visible plain characters only
YEARLY = {"PA", "Per Year"}
CITIZENS_ONLY = re.compile(r"(all )?(united states|u\.s\.|us) citizens", re.IGNORECASE)
TEXT_FIELDS = (
    "JobSummary",
    "MajorDuties",
    "KeyRequirements",
    "Requirements",
    "Education",
    "Evaluations",
    "OtherInformation",
)


def read(fetcher: Any, keywords: list[str]) -> tuple[list[Job], int, bool]:
    """Every keyword's jobs, joined by their link; how many broken entries were skipped; whether the list is whole.

    Any failed request raises, so nothing is saved or closed this run.
    """
    headers = _headers()
    words = [word.strip() for word in keywords if word.strip()]
    if not words:
        raise Skipped(NO_KEYWORDS)
    found, skipped, complete = {}, 0, True
    try:
        for word in words:
            entries, whole = _search(fetcher, word, headers)
            jobs, broken = parse_each(entries, _job)
            found |= {job.source_job_id: job for job in jobs}
            skipped, complete = skipped + broken, complete and whole
    except Refused:
        raise FetchError(KEY_REFUSED) from None
    return list(found.values()), skipped, complete


def _headers() -> dict[str, str]:
    try:
        key, email = get_secret(KEY), get_secret(EMAIL)
    except KeychainError as error:
        raise Skipped(str(error)) from None
    if not (key and email):
        raise Skipped(NEEDS_KEY)
    if not (SENDABLE.fullmatch(key) and SENDABLE.fullmatch(email)):
        raise Skipped(CANT_SEND)
    return {"Authorization-Key": key, "User-Agent": email}


def _search(fetcher: Any, word: str, headers: dict[str, str]) -> tuple[list, bool]:
    """One keyword's entries, page by page, and whether they are all of them."""
    entries = []
    for page in range(1, MAX_PAGES + 1):
        query = urlencode({"Keyword": word, "ResultsPerPage": RESULTS_PER_PAGE, "Page": page})
        data = fetcher.get_json(f"{API}?{query}", headers=headers)[0]
        result = data.get("SearchResult") if isinstance(data, dict) else None
        items = result.get("SearchResultItems") if isinstance(result, dict) else None
        if not isinstance(items, list):
            raise SourceError("unexpected format (no list of jobs)")
        entries += items
        area = result.get("UserArea")
        pages = area.get("NumberOfPages") if isinstance(area, dict) else None
        if isinstance(pages, str) and pages.isdecimal():
            pages = int(pages)
        if type(pages) is not int:  # how far it goes is unknown, so what was not read must not count as gone
            return entries, False
        if page >= pages:
            return entries, True
    return entries, False


def _job(item: dict) -> Job:
    entry = item["MatchedObjectDescriptor"]
    link, details = _link(entry), entry.get("UserArea", {}).get("Details", {})
    places = [
        (html.unescape(place["LocationName"]).strip(), place.get("CountryCode"))
        for place in entry.get("PositionLocation") or []
        if isinstance(place, dict) and isinstance(place.get("LocationName"), str) and place["LocationName"].strip()
    ]
    return Job(
        source=SOURCE,
        source_job_id=link,  # stays with the job when a board's link replaces this one
        company=required_name(entry, "OrganizationName"),
        title=required_name(entry, "PositionTitle"),
        url=link,
        locations=[name for name, _ in places],
        place_checks=[(name, country if isinstance(country, str) else None) for name, country in places],
        sponsorship=_sponsorship(details.get("WhoMayApply")),
        description="\n\n".join(filter(None, (_text(details.get(name)) for name in TEXT_FIELDS))) or None,
        salary=_salary(entry.get("PositionRemuneration")),
        date_posted=day_from_iso(entry.get("PublicationStartDate")),
    )


def _link(entry: dict) -> str:
    """The listing's own address, written one way; anything but https on USAJobs' own site is a broken entry."""
    parts = urlsplit(required_text(entry, "PositionURI"))
    if parts.scheme != "https" or parts.netloc.lower() not in (HOST, f"{HOST}:443") or not parts.path.strip("/"):
        raise ValueError("not a USAJobs listing")
    return f"https://{HOST}{parts.path}"


def _sponsorship(who: Any) -> str:
    name = who.get("Name") if isinstance(who, dict) else who
    return "citizens only" if isinstance(name, str) and CITIZENS_ONLY.fullmatch(name.strip()) else "unknown"


def _salary(pay: Any) -> str | None:
    """A yearly range only: some posts pay by the hour. The numbers arrive as text."""
    first = pay[0] if isinstance(pay, list) and pay else None
    if not isinstance(first, dict) or not {str(first.get("RateIntervalCode")), str(first.get("Description"))} & YEARLY:
        return None
    try:
        return yearly_pay(*(float(str(first.get(end)).replace(",", "")) for end in ("MinimumRange", "MaximumRange")))
    except ValueError:
        return None


def _text(value: Any) -> str:
    parts = value if isinstance(value, list) else [value]
    return "\n".join(filter(None, (html_to_text(html.unescape(part)) for part in parts if isinstance(part, str))))
