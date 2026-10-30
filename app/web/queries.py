"""Read-only job queries for the dashboard: the jobs list with its filters, and the choices those filters offer."""

import json
import math
import re
from dataclasses import asdict, dataclass, replace
from datetime import date
from urllib.parse import parse_qs, urlencode

import sqlcipher3

from app import jobs
from app.collect import DISCOVERED, source_label, source_state
from app.config import AISettings, ScoringSettings
from app.scoring.ready import STATUS
from app.sources import is_simplify_site
from app.sources.boards import BOARDS

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
    "sort": ("posted", "found", "company", "title", "score"),
    "scoring": ("ready", "low", "quick", "failed", "waiting"),
}  # fmt: skip
SCORING_LABELS = {
    "ready": "ready for preparation",
    "low": "low score",
    "quick": "low score (quick check)",
    "failed": "AI score failed: check by hand",
    "waiting": "waiting for scoring",
}
PAGE_NUMBER = re.compile(r"[0-9]{1,9}")  # plain digits only: Python's isdigit() also accepts "²"
SCORE = re.compile(r"[0-9]{1,3}")
SOURCE = re.compile(rf"(simplify|{'|'.join(BOARDS)})(:[\w.-]+)?|manual")  # a kind, or one list or board
SORTS = {
    "posted": "date_posted IS NULL, date_posted DESC, id DESC",  # jobs with no date go last
    "found": "first_seen DESC, id DESC",
    "company": "company COLLATE NOCASE, title COLLATE NOCASE, id",
    "title": "title COLLATE NOCASE, company COLLATE NOCASE, id",
    "score": "s.score IS NULL, s.score DESC, id DESC",  # unscored jobs go last
}
HIDDEN = {"hide": "not_interested = 0", "only": "not_interested = 1", "show": ""}
US_LABELS = {"yes": "US", "unclear": "unclear", "no": "not US"}
# Only columns of the jobs_list index, so listing never reads the long descriptions.
LIST_COLUMNS = """id, title, company, locations, level, remote, us_location, check_location, date_posted, status,
    status_reason, added_by_you, not_interested"""
# Every jobs query joins each job's quick check, its score from the chosen model and a failed new try (when a new
# try failed, the older score stays). The first three values of every query are that model, the ready threshold and
# your resume's fingerprint; a job's scoring status is worked out from them when it is shown.
SCORED = """WITH chosen (model, threshold, resume) AS (SELECT ?, ?, ?)
    SELECT {columns} FROM jobs CROSS JOIN chosen LEFT JOIN stage_a a ON a.job_id = jobs.id
    LEFT JOIN ai_scores s ON s.job_id = jobs.id AND s.model = chosen.model
    LEFT JOIN ai_failures f ON f.job_id = jobs.id AND f.model = chosen.model"""
ODD = "(s.odd IS NOT NULL OR a.trick IS NOT NULL)"
SCORE_COLUMNS = f""", s.score, s.reasons, {ODD} AS odd, a.title_only, s.resume, s.changed AS ad_changed,
    f.failed AS new_try_failed, f.resume AS new_try_resume, {STATUS} AS scoring,
    jobs.prepare_anyway, (SELECT o.model || ': ' || o.score FROM ai_scores o WHERE o.job_id = jobs.id
        AND o.model != chosen.model AND o.score IS NOT NULL ORDER BY o.scored_on DESC LIMIT 1) AS other"""  # noqa: S608 (fixed text)
JOB_COLUMNS = """id, apply_url, company, title, locations, us_location, remote, level, role_category, sponsorship,
    terms, description, salary, date_posted, first_seen, last_seen, status, status_reason, open_manually_only,
    added_by_you, check_location, location_by_you, not_interested"""
JOB_SCORING = f"""{STATUS} AS status, s.score, s.reasons, s.failed, s.odd, s.scored_on, s.resume,
    s.changed AS ad_changed, f.failed AS new_try_failed, f.resume AS new_try_resume, f.tried_on AS new_try_on,
    a.keywords, a.job_skills, a.shared_skills, a.meaning, a.title_only, a.trick, a.passed, a.checked_on,
    jobs.prepare_anyway, (SELECT label FROM answer_key WHERE job_id = jobs.id) AS answer"""  # noqa: S608 (fixed text)
OTHER_SCORES = """SELECT model, score, failed, scored_on FROM ai_scores WHERE job_id = ? AND model != ?
    ORDER BY scored_on DESC"""
SOURCES_OF = """SELECT s.source, s.listed, s.unlisted_on, s.unlisted_reason, r.retired_on, r.last_ok, r.stopped_on,
        r.name IS NOT NULL AND s.source NOT LIKE 'simplify:%'
    FROM job_sources s LEFT JOIN sources r ON r.name = s.source WHERE s.job_id = ? ORDER BY s.rowid"""


@dataclass(frozen=True)
class Scoring:
    """What a job's scoring status depends on: the chosen model, the ready threshold and the current resume."""

    model: str = AISettings().evaluator.model
    threshold: int = ScoringSettings().ready_threshold
    resume: str | None = None  # the resume's fingerprint; None when there is no usable resume

    def values(self) -> list:
        return [self.model, self.threshold, self.resume]


DEFAULT_SCORING = Scoring()


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
    scoring: tuple[str, ...] = ()
    odd: bool = False  # only scores that look odd
    score_from: str = ""  # whole numbers 0 to 100, kept as typed
    score_to: str = ""
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

        def score(name: str) -> str:
            value = one(name)
            return value if SCORE.fullmatch(value) and int(value) <= 100 else ""

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
            scoring=many("scoring"),
            odd=one("odd") == "1",
            score_from=score("score_from"),
            score_to=score("score_to"),
            sort=one("sort") or cls.sort,
            page=max(1, int(page)) if PAGE_NUMBER.fullmatch(page) else 1,
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

    @property
    def first(self) -> int:
        """The number of the first job on this page, counting from 1."""
        return (self.number - 1) * PAGE_SIZE + 1


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
    if view.scoring:
        add(f"{STATUS} IN ({', '.join('?' * len(view.scoring))})", *view.scoring)
    if view.odd:
        add(ODD)
    if view.score_from:
        add("s.score >= ?", int(view.score_from))
    if view.score_to:
        add("s.score <= ?", int(view.score_to))
    return " AND ".join(rules) or "1", values


def list_jobs(conn: sqlcipher3.Connection, view: Filters, scoring: Scoring = DEFAULT_SCORING) -> Page:
    """One page of the jobs matching `view`; a page number past the end shows the last page."""
    where, values = _where(view)
    count = SCORED.format(columns="count(*)")
    total = conn.execute(f"{count} WHERE {where}", scoring.values() + values).fetchone()[0]  # noqa: S608 (fixed rules)
    pages = max(1, math.ceil(total / PAGE_SIZE))
    number = min(view.page, pages)
    select = SCORED.format(columns=LIST_COLUMNS + SCORE_COLUMNS)
    rows = conn.execute(
        f"{select} WHERE {where} ORDER BY {SORTS[view.sort]} LIMIT ? OFFSET ?",  # noqa: S608 (fixed rules)
        [*scoring.values(), *values, PAGE_SIZE, (number - 1) * PAGE_SIZE],
    )
    names = [column[0] for column in rows.description]
    found = [dict(zip(names, row, strict=True)) for row in rows]
    for job in found:
        job["locations"], job["reasons"] = json.loads(job["locations"]), json.loads(job["reasons"] or "[]")
        _resume_marks(job, scoring)
    page = Page(found, total, number, pages)
    if view.status:
        where, values = _where(view, status=False)
        everything = conn.execute(f"{count} WHERE {where}", scoring.values() + values).fetchone()[0]  # noqa: S608
        page.other_statuses = everything - total
    return page


def job_ids(conn: sqlcipher3.Connection, view: Filters, scoring: Scoring, limit: int) -> list[int]:
    """The first `limit` jobs matching `view`, in its order: what "Score these" scores."""
    where, values = _where(view)
    select = SCORED.format(columns="id")
    rows = conn.execute(
        f"{select} WHERE {where} ORDER BY {SORTS[view.sort]} LIMIT ?",  # noqa: S608 (fixed rules)
        [*scoring.values(), *values, limit],
    )
    return [job_id for (job_id,) in rows]


def counts(conn: sqlcipher3.Connection, scoring: Scoring = DEFAULT_SCORING) -> dict[str, int]:
    """Open jobs by scoring status (they add up to `open`), and how many passed the quick check."""
    columns = f"{STATUS}, count(*), coalesce(sum(a.passed), 0)"
    rows = conn.execute(f"{SCORED.format(columns=columns)} WHERE status = 'open' GROUP BY 1", scoring.values())  # noqa: S608
    found = {status: 0 for status in OPTIONS["scoring"]} | {"open": 0, "passed": 0}
    for status, number, passed in rows:
        found[status] += number
        found["open"] += number
        found["passed"] += passed
    return found


def choices(conn: sqlcipher3.Connection) -> dict[str, list[str]]:
    """The values in the database that the category, term and Simplify-list filters can offer."""

    def column(sql: str) -> list[str]:
        return [value for (value,) in conn.execute(sql)]

    return {
        "category": column("SELECT DISTINCT role_category FROM jobs WHERE role_category IS NOT NULL ORDER BY 1"),
        "term": column("SELECT DISTINCT value FROM jobs, json_each(jobs.terms) ORDER BY 1"),
        "simplify": column("SELECT name FROM sources WHERE name LIKE 'simplify:%' ORDER BY 1"),
    }


def job(conn: sqlcipher3.Connection, job_id: int) -> dict | None:
    """One job with every detail and every board or list it is on, each with its own state; None if there is none."""
    rows = conn.execute(f"SELECT {JOB_COLUMNS} FROM jobs WHERE id = ?", (job_id,))  # noqa: S608 (fixed names)
    row = rows.fetchone()
    if row is None:
        return None
    found = dict(zip([column[0] for column in rows.description], row, strict=True))
    found["locations"], found["terms"] = json.loads(found["locations"]), json.loads(found["terms"])
    # checked again here, not only when the job was first saved: the app must never open simplify.jobs
    found["open_manually_only"] = bool(found["open_manually_only"]) or is_simplify_site(found["apply_url"])
    found["sources"] = [
        {
            "label": source_label(source),
            "state": source_state(listed, since, reason),
            "board": _board_state(stopped, retired, read),
            "name": source,
            "stoppable": bool(board),  # a company board, which you can stop reading or read again
            "stopped": bool(stopped),
        }
        for source, listed, since, reason, retired, read, stopped, board in conn.execute(SOURCES_OF, (job_id,))
    ]
    return found


def job_scoring(conn: sqlcipher3.Connection, job_id: int, scoring: Scoring = DEFAULT_SCORING) -> dict:
    """One job's scoring: its status, the chosen model's score, the quick check, other models' scores and your
    answer-key mark. The job must exist."""
    rows = conn.execute(f"{SCORED.format(columns=JOB_SCORING)} WHERE jobs.id = ?", [*scoring.values(), job_id])  # noqa: S608
    found = dict(zip([column[0] for column in rows.description], rows.fetchone(), strict=True))
    for name in ("reasons", "keywords", "shared_skills"):
        found[name] = json.loads(found[name] or "[]")
    found["label"], found["model"] = SCORING_LABELS[found["status"]], scoring.model
    _resume_marks(found, scoring)
    found["others"] = conn.execute(OTHER_SCORES, (job_id, scoring.model)).fetchall()
    return found


def _resume_marks(job: dict, scoring: Scoring) -> None:
    """Marks that depend on the current resume: scored with an older one, and a failed new try made with this one."""
    job["older"] = bool(scoring.resume and job["resume"] and job["resume"] != scoring.resume)
    if job.pop("new_try_resume") != scoring.resume:  # a try with another resume no longer applies
        job["new_try_failed"] = None


def _board_state(stopped: str | None, retired: str | None, read: str | None) -> str:
    """The board's own state: stopped by you, no longer read, or when it was last read."""
    if stopped:
        return f"stopped by you on {stopped}"
    if retired:
        return f"board no longer read since {retired}"
    return f"last read {read[:10]}" if read else ""


def job_id_by_link(conn: sqlcipher3.Connection, link: str) -> int | None:
    """The saved job with this link, cleaned the way saving cleans it."""
    row = conn.execute("SELECT id FROM jobs WHERE apply_url = ?", (jobs.clean_url(link),)).fetchone()
    return row[0] if row else None


BOARD_SHOWS = {  # the Boards page's choices: which boards to list
    "all": "1",
    "active": f"stopped_on IS NULL AND retired_on IS NULL AND NOT ({DISCOVERED})",
    "discovered": f"stopped_on IS NULL AND retired_on IS NULL AND {DISCOVERED}",
    "stopped": "stopped_on IS NOT NULL",
    "retired": "retired_on IS NOT NULL",
}
BOARD_ROWS = f"""SELECT name, found_by, company, last_ok, stopped_on, retired_on, {DISCOVERED},
        (SELECT count(*) FROM job_sources WHERE source = sources.name AND listed = 1)
    FROM sources"""  # noqa: S608 (fixed text)
BOARDS_PAGE_SIZE = 100


@dataclass(frozen=True)
class BoardView:
    """One view of the Boards page, as kept in the page address."""

    q: str = ""  # text in the board's name or company name
    show: str = "all"
    page: int = 1

    @classmethod
    def from_address(cls, params: dict[str, str]) -> "BoardView":
        show, page = params.get("show", ""), params.get("page", "")
        return cls(
            q=params.get("q", "").strip()[:MAX_TEXT],
            show=show if show in BOARD_SHOWS else cls.show,
            page=max(1, int(page)) if PAGE_NUMBER.fullmatch(page) else 1,
        )

    def address(self, **changes) -> str:
        view = replace(self, **changes)
        pairs = {
            "q": view.q,
            "show": view.show if view.show != "all" else "",
            "page": view.page if view.page > 1 else "",
        }
        return "/boards?" + urlencode({name: value for name, value in pairs.items() if value})


def found_by_label(found_by: str | None) -> str:
    """Who found a board, in the page's words."""
    labels = {None: "before the phone book", "simplify": "Simplify", "you": "you", "commoncrawl": "Common Crawl"}
    return labels.get(found_by) or (f"list {found_by[5:]}" if found_by.startswith("list:") else found_by)


def list_boards(conn: sqlcipher3.Connection, view: BoardView) -> tuple[list[dict], int, int, int]:
    """One page of the company boards matching `view` (never Simplify's lists).

    Returns the boards, how many match, this page's number and the number of pages; past the end is the last page.
    """
    where = f"name NOT LIKE 'simplify:%' AND {BOARD_SHOWS[view.show]}"
    values: list = []
    if view.q:
        where += " AND (instr(lower(name), ?) OR instr(lower(ifnull(company, '')), ?))"
        values += [view.q.lower()] * 2
    total = conn.execute(f"SELECT count(*) FROM sources WHERE {where}", values).fetchone()[0]  # noqa: S608
    pages = max(1, math.ceil(total / BOARDS_PAGE_SIZE))
    number = min(view.page, pages)
    rows = conn.execute(
        f"{BOARD_ROWS} WHERE {where} ORDER BY name LIMIT ? OFFSET ?",  # noqa: S608 (fixed rules)
        [*values, BOARDS_PAGE_SIZE, (number - 1) * BOARDS_PAGE_SIZE],
    )
    boards = []
    for name, found_by, company, last_ok, stopped_on, retired_on, discovered, jobs_listed in rows:
        boards.append({
            "name": name, "company": company, "found_by": found_by_label(found_by), "last_read": (last_ok or "")[:10],
            "stopped_on": stopped_on, "retired_on": retired_on, "discovered": bool(discovered), "jobs": jobs_listed,
            "page": _board_page(name),
        })  # fmt: skip
    return boards, total, number, pages


def board_counts(conn: sqlcipher3.Connection) -> dict[str, int]:
    """How many company boards each choice of the Boards page lists."""
    sums = ", ".join(f"sum({rule})" for rule in BOARD_SHOWS.values())
    row = conn.execute(f"SELECT {sums} FROM sources WHERE name NOT LIKE 'simplify:%'").fetchone()  # noqa: S608
    return {show: count or 0 for show, count in zip(BOARD_SHOWS, row, strict=True)}


def empty_boards(conn: sqlcipher3.Connection) -> int:
    """Boards the phone book found that exist but list no jobs yet; each refresh round asks them again."""
    return conn.execute("SELECT count(*) FROM phonebook WHERE result = 'empty'").fetchone()[0]


def waiting_boards(conn: sqlcipher3.Connection) -> list[dict]:
    """Boards the cross-check found that wait for you to add or decline them."""
    rows = conn.execute(  # a board that reached the boards list another way (e.g. through Simplify) waits no more
        "SELECT board, company, cross_of FROM phonebook WHERE result = 'to confirm' "
        "AND NOT EXISTS (SELECT 1 FROM sources WHERE name = board) ORDER BY board"
    ).fetchall()
    return [
        {"name": board, "company": company, "cross_of": cross_of, "page": _board_page(board)}
        for board, company, cross_of in rows
    ]


def _board_page(board: str) -> str | None:
    """The board's own public page, e.g. https://jobs.lever.co/acme for lever:acme."""
    kind, _, name = board.partition(":")
    return BOARDS[kind].BOARD_PAGE.format(name) if kind in BOARDS else None


def last_runs(conn: sqlcipher3.Connection, count: int = 5) -> list[dict]:
    """The latest collections, newest first, with how many sources failed or need investigation."""
    rows = conn.execute(
        "SELECT started_at, outcome, new_jobs, updated_jobs, closed_jobs, failures FROM collect_runs "
        "ORDER BY id DESC LIMIT ?",
        (count,),
    )
    return [
        {"started": started[:16].replace("T", " "), "outcome": outcome or "not finished (running, or interrupted)",
         "new": new, "updated": updated, "unavailable": unavailable, "problems": len(json.loads(failures))}
        for started, outcome, new, updated, unavailable, failures in rows
    ]  # fmt: skip


def last_refreshes(conn: sqlcipher3.Connection, count: int = 3) -> list[dict]:
    """The latest phone-book refreshes, newest first, each with the last line of its summary."""
    rows = conn.execute("SELECT started_at, outcome, summary FROM phonebook_runs ORDER BY id DESC LIMIT ?", (count,))
    return [
        {"started": started[:16].replace("T", " "), "outcome": outcome or "not finished (running, or interrupted)",
         "summary": summary.splitlines()[-1] if summary else ""}
        for started, outcome, summary in rows
    ]  # fmt: skip


def stopped_boards(conn: sqlcipher3.Connection) -> list[tuple[str, str]]:
    """Boards you stopped reading, with the day you stopped them."""
    return conn.execute("SELECT name, stopped_on FROM sources WHERE stopped_on IS NOT NULL ORDER BY name").fetchall()
