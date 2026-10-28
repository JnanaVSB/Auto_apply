"""The phone book of company boards: `python -m app.phonebook refresh` finds more boards in public lists.

The lists: every job-board link in Simplify's lists (closed jobs too), in the other GitHub job lists you approved
(phonebook_lists in data/config.yaml), and in Common Crawl's index of the web.

Every name found waits in the phonebook table until its site's own API confirms it; a confirmed board is read on every
collection from then on. A new name is also tried once on the other sites (the cross-check). Nothing is deleted, and an
interrupted refresh continues where it stopped.
"""

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from contextlib import closing, suppress
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import sqlcipher3

from app import collect, db, jobs
from app.collect import CollectError, Download, Output, now
from app.config import Config, ConfigError, load_config
from app.fetch import COLLECT_HOSTS, COMMON_CRAWL, Fetcher, FetchError, Guarded, NotFound, Unreachable
from app.keychain import KeychainError
from app.sources import BOARD_HOSTS, SourceError, board_of, greenhouse, simplify
from app.sources.boards import BOARDS

CRAWLS = f"https://{COMMON_CRAWL}/collinfo.json"
CRAWL_ID = re.compile(r"CC-MAIN-[0-9]{4}-[0-9]{2}")
CRAWLS_TO_READ = 3  # the newest monthly crawls; each is read once, so a monthly refresh reads one new crawl
SAME_TITLES = 3  # job titles two boards must share for the cross-check to call them one company
PROGRESS_EVERY = 100
OVERLOADED_AFTER = 2  # site parts in a row that fail before Common Crawl is left for this refresh
PAGES_NOTE_EVERY = 10  # a progress line every this many index pages
READING = (
    "Reading Common Crawl's index, one site at a time. It is slow on purpose and often busy: a busy part can take up"
    " to 4 minutes of waiting before it is left for next time."
)
OVERLOADED = (
    f"Common Crawl's index seems overloaded ({OVERLOADED_AFTER} parts in a row failed): the rest of it is read at the"
    " next refresh. Checking the names found so far."
)
RETRY_AFTER = timedelta(days=1)  # a round a failing source left unfinished is continued at most once a day
PAUSED = "Paused so a collection can run; the refresh continues where it stopped once the collection has finished."
# A name is added once, and never when it is already a board the app reads. A real sighting (not the cross-check) of a
# name that only the cross-check found "not found" makes it a new name again, asked once more.
ADD_NAME = """INSERT INTO phonebook (board, found_by, found_on, cross_of) SELECT :board, :found_by, :on, :cross_of
    WHERE NOT EXISTS (SELECT 1 FROM sources WHERE name = :board)
    ON CONFLICT (board) DO UPDATE SET found_by = excluded.found_by, found_on = excluded.found_on, cross_of = NULL,
        checked_on = NULL, result = NULL
    WHERE phonebook.found_by = 'cross-check' AND phonebook.result = 'not found'
        AND excluded.found_by != 'cross-check'"""
# Names to ask: never asked yet, or empty when last asked before this round began (round_start).
TO_CHECK = """SELECT board, found_by, cross_of FROM phonebook
    WHERE checked_on IS NULL OR (result = 'empty' AND checked_on < ?) ORDER BY board"""
DONE = "INSERT INTO phonebook_done (part, done_on) VALUES (?, ?) ON CONFLICT (part) DO NOTHING"
LINK = re.compile(r"https?://[^\s\"'<>()\[\]|\\]+")  # any web address in a list file, JSON or Markdown


class Paused(Exception):
    """A collection is waiting for the shared lock: the refresh stops before its next request."""


def _pause_if_asked(config: Config) -> None:
    if collect.collection_waiting(config.data_dir):
        raise Paused


def _round(conn: sqlcipher3.Connection) -> tuple[str | None, str | None, str | None]:
    """When the current round began, and the last refresh's start and outcome (all None before the first one).

    A round is one or more refreshes: it starts fresh, and every refresh that continues it after a pause, a stop or a
    failing source belongs to it, until one ends as "done".
    """
    row = conn.execute(
        "SELECT (SELECT max(started_at) FROM phonebook_runs WHERE continues = 0), started_at, outcome "
        "FROM phonebook_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row or (None, None, None)


def continues_round(conn: sqlcipher3.Connection, every_days: int, now: datetime) -> bool:
    """A refresh continues the current round when the last one left it unfinished, unless the round began
    `every_days` ago or more (0 = never too old): then a new round starts."""
    round_start, _, outcome = _round(conn)
    if round_start is None or outcome == "done":
        return False
    return not every_days or now - datetime.fromisoformat(round_start) < timedelta(days=every_days)


def due(conn: sqlcipher3.Connection, every_days: int, now: datetime) -> bool:
    """The schedule: a new round when the current one began `every_days` ago or more (or there was none); an
    unfinished round continues at the next chance, or after a day when a source failed."""
    round_start, last_start, outcome = _round(conn)
    if round_start is None or now - datetime.fromisoformat(round_start) >= timedelta(days=every_days):
        return True
    if outcome == "done":
        return False
    return outcome != "partial" or now - datetime.fromisoformat(last_start) >= RETRY_AFTER


def _add_names(conn: sqlcipher3.Connection, names: set[str], found_by: str) -> int:
    """Add names to the phone book (inside the caller's transaction); returns how many were new."""
    found_on = now()
    values = [{"board": board, "found_by": found_by, "on": found_on, "cross_of": None} for board in sorted(names)]
    return sum(conn.execute(ADD_NAME, value).rowcount for value in values)


def _done(conn: sqlcipher3.Connection, part: str) -> bool:
    return conn.execute("SELECT 1 FROM phonebook_done WHERE part = ?", (part,)).fetchone() is not None


def _newest_crawls(data: Any) -> list[str]:
    """The newest crawls' names, e.g. CC-MAIN-2026-40, from Common Crawl's list of crawls (newest first)."""
    names = [crawl.get("id") for crawl in data if isinstance(crawl, dict)] if isinstance(data, list) else []
    names = [name for name in names if isinstance(name, str) and CRAWL_ID.fullmatch(name)]
    if not names:
        raise SourceError("its list of crawls looks different from usual")
    return sorted(names, reverse=True)[:CRAWLS_TO_READ]


def _names(text: str) -> tuple[set[str], int, int]:
    """Board names from one index page (one JSON line per web address), how many lines were read, and how many could
    not be."""
    names, read, damaged = set(), 0, 0
    for line in filter(str.strip, text.splitlines()):
        try:
            board = board_of(json.loads(line)["url"])
        except (ValueError, KeyError, TypeError, AttributeError):
            damaged += 1
            continue
        read += 1
        if board:
            names.add(board)
    return names, read, damaged


def read_common_crawl(conn: sqlcipher3.Connection, fetcher: Fetcher, out: Output) -> tuple[int, int, int]:
    """Take board names from the newest crawls' index, one page at a time, each page saved as it is read.

    A site whose part fails is skipped until the next refresh, and said at once; after OVERLOADED_AFTER failed parts
    in a row, or when the index stops answering, the rest waits for the next refresh (each failure already took
    minutes of retries). Returns new names, damaged index lines, and how many parts failed.
    """
    new = damaged = failed = in_a_row = 0
    for crawl in _newest_crawls(fetcher.get_json(CRAWLS)[0]):
        for host in BOARD_HOSTS:
            part = f"commoncrawl:{crawl}:{host}"
            if _done(conn, part):
                continue
            if not new and not damaged and not failed:  # said once, before the first part to read
                out(READING)
            try:
                found, bad = _read_part(conn, fetcher, crawl, host, part, out)
            except Unreachable:
                raise
            except (FetchError, SourceError) as error:
                out(f"Common Crawl {crawl}, {host} could not be read ({error}); tried again next time.")
                failed, in_a_row = failed + 1, in_a_row + 1
                if in_a_row == OVERLOADED_AFTER:
                    out(OVERLOADED)
                    return new, damaged, failed
                continue
            with conn:
                conn.execute(DONE, (part, now()))
            new, damaged, in_a_row = new + found, damaged + bad, 0
            out(f"Common Crawl {crawl}, {host}: read ({new:,} new names so far)")
    return new, damaged, failed


def _read_part(
    conn: sqlcipher3.Connection, fetcher: Fetcher, crawl: str, host: str, part: str, out: Output
) -> tuple[int, int]:
    """One site's pages in one crawl; "not found" means the index has nothing there. New names and damaged lines."""
    query = f"https://{COMMON_CRAWL}/{crawl}-index?url={host}/*&output=json&fl=url"
    try:
        pages = fetcher.get_json(f"{query}&showNumPages=true")[0]
    except NotFound:
        return 0, 0
    count = pages.get("pages") if isinstance(pages, dict) else None
    if type(count) is not int or count < 0:  # true and false are not counts
        raise SourceError("its page count looks different from usual")
    new = damaged = 0
    out(f"Common Crawl {crawl}, {host}: {count:,} index page{'' if count == 1 else 's'} to read")
    for page in range(count):
        if _done(conn, f"{part}:{page}"):
            continue
        if page and page % PAGES_NOTE_EVERY == 0:
            out(f"  page {page + 1:,} of {count:,}")
        try:
            text = fetcher.get_text(f"{query}&page={page}")
        except NotFound:
            text = ""
        names, read, bad = _names(text)
        if bad and not read:  # not index lines at all, e.g. an error page: asked again next time
            raise SourceError(f"page {page + 1} is not index lines (perhaps an error page)")
        with conn:  # the page's names and "page done" are saved together
            new += _add_names(conn, names, "commoncrawl")
            conn.execute(DONE, (f"{part}:{page}", now()))
        damaged += bad
    return new, damaged


def read_lists(conn: sqlcipher3.Connection, fetcher: Fetcher, config: Config, out: Output) -> list[str]:
    """Take board names from every job-board link in Simplify's lists and in your phone-book lists.

    Returns a line for each list that could not be read.
    """
    lists = [(listing.url, "simplify") for listing in config.sources.simplify_lists]
    lists += [(url, "list:" + "/".join(urlsplit(url).path.split("/")[1:3])) for url in config.sources.phonebook_lists]
    failed = []
    for url, found_by in lists:  # found_by names the list, e.g. list:vanshb03/New-Grad-2027
        label = collect.source_label(simplify.source_name(url)) if found_by == "simplify" else found_by[5:]
        try:
            text = fetcher.get_text(url)
        except FetchError as error:
            failed.append(f"{label} could not be read ({error}); try again later.")
            continue
        names = {board for board in map(board_of, LINK.findall(text)) if board}
        with conn:
            found = _add_names(conn, names, found_by)
        out(f"{label}: {len(names):,} boards linked, {found:,} new names")
    return failed


def _titles(titles: list[str]) -> set[str]:
    return {" ".join(title.casefold().split()) for title in titles}


def _titles_of(conn: sqlcipher3.Connection, board: str, titles: dict[str, set[str]]) -> set[str]:
    """A board's job titles: from this refresh, or else the titles of the jobs saved from it (e.g. after a stop)."""
    if board not in titles:
        rows = conn.execute(
            "SELECT j.title FROM jobs j JOIN job_sources s ON s.job_id = j.id WHERE s.source = ?", (board,)
        )
        titles[board] = _titles([title for (title,) in rows])
    return titles[board]


def _check(
    conn: sqlcipher3.Connection,
    board: str,
    name: tuple[str, str | None],
    download: Download | None,
    titles: dict[str, set[str]],
    roles: jobs.Roles,
) -> str:
    """Record what the site's API said about one name; returns the result, as the summary counts it."""
    found_by, cross_of = name
    if isinstance(download, NotFound):
        result, found, company = "not found", [], None
    elif download is None or isinstance(download, FetchError | SourceError):
        return "could not be checked"  # stays unchecked, so the next refresh asks again
    elif isinstance(download, BaseException):
        raise download  # Ctrl+C, or a fault in the app
    elif not download[0]:  # it exists but lists no jobs: added only once it does (as with Add company)
        result, found, company = "empty", [], None
    else:
        found = download[0]
        kind, _, short = board.partition(":")
        company = greenhouse.reported_company(found, short) if kind == "greenhouse" else None  # only Greenhouse says
        titles[board] = _titles([job.title for job in found])
        same = found_by != "cross-check" or len(_titles_of(conn, cross_of, titles) & titles[board]) >= SAME_TITLES
        result = "added" if same else "to confirm"
    checked = now()
    with conn:
        sql = "UPDATE phonebook SET checked_on = ?, result = ?, company = ? WHERE board = ?"
        conn.execute(sql, (checked, result, company, board))
        if result == "added":
            sql = "INSERT INTO sources (name, found_by, company, last_ok) VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING"
            conn.execute(sql, (board, found_by, company, checked))
            kind, _, short = board.partition(":")
            if found_by != "cross-check":  # the same name on the other sites, tried once
                for other in BOARDS.keys() - {kind}:
                    values = {"board": f"{other}:{short}", "found_by": "cross-check", "on": checked, "cross_of": board}
                    conn.execute(ADD_NAME, values)
    if result == "added" and found:  # its matching jobs are saved now, as a collection would save them
        jobs.save(conn, board, found, roles, checked)
    return result


def check_names(conn: sqlcipher3.Connection, fetcher: Fetcher, roles: jobs.Roles, out: Output) -> Counter:
    """Ask each site's API about every name not checked yet, all sites at the same time.

    Then once more for the names the cross-check added meanwhile; a name is asked at most once per refresh.
    """
    results, titles, asked = Counter(), {}, set()  # titles: each board's job titles, for the cross-check
    round_start = _round(conn)[0]
    for _ in range(2):
        rows = conn.execute(TO_CHECK, (round_start,))
        names = {board: (found_by, cross_of) for board, found_by, cross_of in rows if board not in asked}
        asked |= names.keys()
        if not names:
            break
        out(f"Checking {len(names):,} names with each site's own API...")
        with closing(collect.download_boards(fetcher, list(names), set())) as downloads:
            for number, (_, board, download) in enumerate(downloads, 1):
                results[_check(conn, board, names[board], download, titles, roles)] += 1
                if number % PROGRESS_EVERY == 0:
                    out(f"  {number:,} of {len(names):,} checked")
    return results


def refresh(conn: sqlcipher3.Connection, config: Config, fetcher: Fetcher | Guarded, out: Output = print) -> str:
    """Find new board names, check them, and record the refresh; what is done is kept if it stops midway.

    Returns the outcome: done, partial (a source failed), or paused (a collection is waiting; the next refresh
    continues the same round).
    """
    if not any(fetcher.reachable(site) for site in sorted(COLLECT_HOSTS)):
        raise CollectError("No internet connection: none of the job sites could be reached. Check it and try again.")
    fetcher = Guarded(fetcher, lambda: _pause_if_asked(config))
    with conn:
        continues = continues_round(conn, config.phonebook_every_days, datetime.now(UTC))
        sql = "INSERT INTO phonebook_runs (started_at, continues) VALUES (?, ?)"
        run_id = conn.execute(sql, (now(), continues)).lastrowid
    lines: list[str] = []
    outcome = "stopped"

    def say(line: str) -> None:
        lines.append(line)
        out(line)

    try:
        failed = read_lists(conn, fetcher, config, say)
        for line in failed:
            say(line)
        complete = not failed
        try:
            new, damaged, failed = read_common_crawl(conn, fetcher, say)  # failed parts are said as they happen
            complete = complete and not failed
            say(f"Common Crawl: {new:,} new names" + (f" ({damaged:,} damaged index lines skipped)" if damaged else ""))
        except (FetchError, SourceError) as error:
            complete = False
            say(f"Common Crawl's index could not be read ({error}). Names found so far are kept; try again later.")
        results = check_names(conn, fetcher, jobs.Roles.from_settings(config.roles), say)
        say(
            f"Checked {results.total():,} names: {results['added']:,} added (read on every collection from now on), "
            f"{results['empty']:,} with no jobs yet (asked again next round), {results['not found']:,} not found, "
            f"{results['to confirm']:,} to confirm on the Boards page, "
            f"{results['could not be checked']:,} could not be checked (asked again next time)."
        )
        outcome = "done" if complete else "partial"
    except Paused:
        outcome = "paused"
        say(PAUSED)
    except KeyboardInterrupt:
        outcome = "cancelled"
        raise
    finally:
        with suppress(sqlcipher3.Error), conn:  # a broken database must not hide the original error
            conn.execute(
                "UPDATE phonebook_runs SET finished_at = ?, outcome = ?, summary = ? WHERE id = ?",
                (now(), outcome, "\n".join(lines), run_id),
            )
    return outcome


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.phonebook", description="The phone book of company boards.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("refresh", help="find more company boards (slow on purpose; safe to stop and run again)")
    parser.parse_args(argv)
    try:
        config = load_config()
        with (
            closing(collect.lock(config.data_dir, collect.REFRESH_LOCK)),  # one refresh at a time, paused or not
            closing(Fetcher(per_second=config.requests_per_second)) as fetcher,
        ):
            shared = collect.lock(config.data_dir)  # no collection runs meanwhile, except while paused
            while True:
                with closing(shared), closing(db.connect(config.data_dir)) as conn:
                    if refresh(conn, config, fetcher) != "paused":
                        break
                shared = collect.wait_for_collection(config.data_dir)
    except (
        ConfigError,
        KeychainError,
        db.DatabaseError,
        CollectError,
        sqlcipher3.Error,
        sqlite3.Error,
        OSError,
    ) as error:
        print(db.error_line(error), file=sys.stderr)
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled. Names found and checked so far are kept; run it again to continue.", file=sys.stderr)
    else:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
