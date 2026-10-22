"""Read-only job queries for the dashboard: the jobs list with its filters, and the choices those filters offer."""

import json
import math
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from urllib.parse import parse_qs, urlencode

import sqlcipher3

from app import jobs

PAGE_SIZE = 50
MAX_TEXT = 200
OPTIONS = {  # every value a list filter accepts; anything else in the address is ignored
    "level": ("intern", "new grad", "entry", "mid", "senior", "staff+", "unknown"),
    "us": ("yes", "unclear", "no"),
    "remote": ("remote", "hybrid", "on-site", "unknown"),
    "sponsorship": ("offers", "no", "citizens only", "unknown"),
    "status": ("open", "no longer available", "closed"),
    "reason": (
        jobs.REMOVED_FROM_BOARD, jobs.BOARD_SHUT_DOWN, jobs.STOPPED_HIRING, jobs.REMOVED_FROM_SIMPLIFY,
        jobs.INACTIVE, jobs.NOT_RECORDED,
    ),
    "hidden": ("hide", "show", "only"),
    "sort": ("posted", "found", "company", "title"),
}  # fmt: skip
SOURCE = re.compile(r"(simplify|greenhouse|lever|ashby)(:[\w.-]+)?|manual")  # a kind, or one list or board
SORTS = {
    "posted": "date_posted IS NULL, date_posted DESC, id DESC",  # jobs with no date go last
    "found": "first_seen DESC, id DESC",
    "company": "company COLLATE NOCASE, title COLLATE NOCASE, id",
    "title": "title COLLATE NOCASE, company COLLATE NOCASE, id",
}
HIDDEN = {"hide": "not_interested = 0", "only": "not_interested = 1", "show": ""}
# Only columns of the jobs_list index, so listing never reads the long descriptions.
LIST_COLUMNS = """id, title, company, locations, level, remote, us_location, check_location, date_posted, status,
    status_reason, added_by_you, not_interested"""


@dataclass(frozen=True)
class Filters:
    """One view of the jobs list, as kept in the page address."""

    q: str = ""  # words in the title or company
    source: str = ""
    level: tuple[str, ...] = ()
    us: tuple[str, ...] = ()
    check: bool = False  # only jobs marked "please check location"
    remote: tuple[str, ...] = ()
    posted_from: str = ""
    posted_to: str = ""
    category: str = ""
    term: str = ""
    sponsorship: tuple[str, ...] = ()
    status: tuple[str, ...] = ("open",)  # until the filter form is used
    reason: str = ""
    added: bool = False
    hidden: str = "hide"
    sort: str = "posted"
    page: int = 1

    @classmethod
    def from_address(cls, query: str) -> "Filters":
        try:
            params = parse_qs(query, max_num_fields=200)
        except ValueError:  # an absurd address: the default view
            return cls()

        def one(name: str) -> str:
            value = params.get(name, [""])[-1].strip()[:MAX_TEXT]
            return value if name not in OPTIONS or value in OPTIONS[name] else ""

        def many(name: str) -> tuple[str, ...]:
            return tuple(value for value in OPTIONS[name] if value in params.get(name, []))

        def day(name: str) -> str:
            try:
                return date.fromisoformat(one(name)).isoformat()
            except ValueError:
                return ""

        page, source = one("page"), one("board") or one("source")  # a typed board wins over the source list
        return cls(
            q=one("q"),
            source=source if SOURCE.fullmatch(source) else "",
            level=many("level"),
            us=many("us"),
            check=one("check") == "1",
            remote=many("remote"),
            posted_from=day("posted_from"),
            posted_to=day("posted_to"),
            category=one("category"),
            term=one("term"),
            sponsorship=many("sponsorship"),
            status=many("status") if "f" in params else cls.status,  # "f": the filter form was used
            reason=one("reason"),
            added=one("added") == "1",
            hidden=one("hidden") or cls.hidden,
            sort=one("sort") or cls.sort,
            page=max(1, int(page)) if page.isdigit() else 1,
        )

    def address(self, **changes) -> str:
        """This view's address, with some values changed (a page, a sort)."""
        view, pairs = replace(self, **changes), [("f", "1")]
        for name, value in asdict(view).items():
            if isinstance(value, tuple):
                pairs += [(name, item) for item in value]
            elif value and value != getattr(Filters, name):
                pairs.append((name, "1" if value is True else value))
        return "/?" + urlencode(pairs)


@dataclass
class Page:
    jobs: list[dict]
    total: int  # jobs matching every filter
    number: int
    pages: int
    other_statuses: int = 0  # jobs left out only by the status filter
    first: int = field(init=False)

    def __post_init__(self) -> None:
        self.first = (self.number - 1) * PAGE_SIZE + 1


def _where(view: Filters, *, status: bool = True) -> tuple[str, list]:
    rules, values = [], []

    def add(rule: str, *more) -> None:
        rules.append(rule)
        values.extend(more)

    def any_of(column: str, chosen: tuple[str, ...]) -> None:
        if chosen:
            add(f"{column} IN ({', '.join('?' * len(chosen))})", *chosen)

    if HIDDEN[view.hidden]:
        add(HIDDEN[view.hidden])
    if view.q:
        words = "%" + re.sub(r"([\\%_])", r"\\\1", view.q) + "%"  # typed % and _ are ordinary characters
        add("(title LIKE ? ESCAPE '\\' OR company LIKE ? ESCAPE '\\')", words, words)
    # "+id": keeps SQLite on the speed index instead of looking up each job in the table (which reads descriptions)
    if view.source == "manual" or ":" in view.source:
        add("+id IN (SELECT job_id FROM job_sources WHERE source = ?)", view.source)
    elif view.source:  # every list or board of that kind
        add(
            "+id IN (SELECT job_id FROM job_sources WHERE source >= ? AND source < ?)",
            view.source + ":",
            view.source + ";",
        )
    any_of("level", view.level)
    any_of("us_location", view.us)
    any_of("remote", view.remote)
    any_of("sponsorship", view.sponsorship)
    if status:
        any_of("status", view.status)
    if view.check:
        add("check_location = 1")
    if view.posted_from:
        add("date_posted >= ?", view.posted_from)  # a job with no date never matches a date range
    if view.posted_to:
        add("date_posted <= ?", view.posted_to)
    if view.category:
        add("role_category = ?", view.category)
    if view.term:
        add("EXISTS (SELECT 1 FROM json_each(terms) WHERE value = ?)", view.term)
    if view.reason:
        add("status_reason = ?", view.reason)
    if view.added:
        add("added_by_you = 1")
    return " AND ".join(rules) or "1", values


def list_jobs(conn: sqlcipher3.Connection, view: Filters) -> Page:
    """One page of the jobs matching `view`; a page number past the end shows the last page."""
    where, values = _where(view)
    total = conn.execute(f"SELECT count(*) FROM jobs WHERE {where}", values).fetchone()[0]  # noqa: S608 (fixed rules)
    pages = max(1, math.ceil(total / PAGE_SIZE))
    number = min(view.page, pages)
    rows = conn.execute(
        f"SELECT {LIST_COLUMNS} FROM jobs WHERE {where} ORDER BY {SORTS[view.sort]} LIMIT ? OFFSET ?",  # noqa: S608
        [*values, PAGE_SIZE, (number - 1) * PAGE_SIZE],
    )
    names = [column[0] for column in rows.description]
    found = [dict(zip(names, row, strict=True)) for row in rows]
    for job in found:
        job["locations"] = json.loads(job["locations"])
    page = Page(found, total, number, pages)
    if view.status:
        where, values = _where(view, status=False)
        everything = conn.execute(f"SELECT count(*) FROM jobs WHERE {where}", values).fetchone()[0]  # noqa: S608
        page.other_statuses = everything - total
    return page


def choices(conn: sqlcipher3.Connection) -> dict[str, list[str]]:
    """The values in the database that the category, term and Simplify-list filters can offer."""

    def column(sql: str) -> list[str]:
        return [value for (value,) in conn.execute(sql)]

    return {
        "category": column("SELECT DISTINCT role_category FROM jobs WHERE role_category IS NOT NULL ORDER BY 1"),
        "term": column("SELECT DISTINCT value FROM jobs, json_each(jobs.terms) ORDER BY 1"),
        "simplify": column("SELECT name FROM sources WHERE name LIKE 'simplify:%' ORDER BY 1"),
    }
