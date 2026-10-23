"""The phone book of company boards: `python -m app.phonebook refresh` finds more boards in Common Crawl's index.

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
from typing import Any

import sqlcipher3

from app import collect, db, jobs
from app.collect import CollectError, Download, Output, now
from app.config import ConfigError, load_config
from app.fetch import COMMON_CRAWL, Fetcher, FetchError, NotFound
from app.keychain import KeychainError
from app.sources import BOARD_HOSTS, Job, SourceError, board_of
from app.sources.boards import BOARDS

CRAWLS = f"https://{COMMON_CRAWL}/collinfo.json"
CRAWL_ID = re.compile(r"CC-MAIN-[0-9]{4}-[0-9]{2}")
CRAWLS_TO_READ = 3  # the newest monthly crawls; each is read once, so a monthly refresh reads one new crawl
SAME_TITLES = 3  # job titles two boards must share for the cross-check to call them one company
PROGRESS_EVERY = 100
# A name is added once, and never when it is already a board the app reads.
ADD_NAME = """INSERT INTO phonebook (board, found_by, found_on, cross_of) SELECT :board, :found_by, :on, :cross_of
    WHERE NOT EXISTS (SELECT 1 FROM sources WHERE name = :board) ON CONFLICT (board) DO NOTHING"""
DONE = "INSERT INTO phonebook_done (part, done_on) VALUES (?, ?) ON CONFLICT (part) DO NOTHING"


def _done(conn: sqlcipher3.Connection, part: str) -> bool:
    return conn.execute("SELECT 1 FROM phonebook_done WHERE part = ?", (part,)).fetchone() is not None


def _newest_crawls(data: Any) -> list[str]:
    """The newest crawls' names, e.g. CC-MAIN-2026-40, from Common Crawl's list of crawls (newest first)."""
    names = [crawl.get("id") for crawl in data if isinstance(crawl, dict)] if isinstance(data, list) else []
    names = [name for name in names if isinstance(name, str) and CRAWL_ID.fullmatch(name)]
    if not names:
        raise SourceError("its list of crawls looks different from usual")
    return sorted(names, reverse=True)[:CRAWLS_TO_READ]


def _names(text: str) -> tuple[set[str], int]:
    """Board names from one index page (one JSON line per web address), and how many lines could not be read."""
    names, damaged = set(), 0
    for line in filter(str.strip, text.splitlines()):
        try:
            board = board_of(json.loads(line)["url"])
        except (ValueError, KeyError, TypeError, AttributeError):
            damaged += 1
            continue
        if board:
            names.add(board)
    return names, damaged


def read_common_crawl(conn: sqlcipher3.Connection, fetcher: Fetcher, out: Output) -> tuple[int, int]:
    """Take board names from the newest crawls' index, one page at a time, each page saved as it is read.

    Returns how many new names were found and how many index lines were damaged.
    """
    new = damaged = 0
    for crawl in _newest_crawls(fetcher.get_json(CRAWLS)[0]):
        for host in BOARD_HOSTS:
            part = f"commoncrawl:{crawl}:{host}"
            if _done(conn, part):
                continue
            query = f"https://{COMMON_CRAWL}/{crawl}-index?url={host}/*&output=json&fl=url"
            pages = fetcher.get_json(f"{query}&showNumPages=true")[0]
            if not isinstance(pages, dict) or not isinstance(pages.get("pages"), int):
                raise SourceError("its page count looks different from usual")
            for page in range(pages["pages"]):
                if _done(conn, f"{part}:{page}"):
                    continue
                names, bad = _names(fetcher.get_text(f"{query}&page={page}"))
                found_on = now()
                with conn:  # the page's names and "page done" are saved together
                    for board in sorted(names):
                        values = {"board": board, "found_by": "commoncrawl", "on": found_on, "cross_of": None}
                        new += conn.execute(ADD_NAME, values).rowcount
                    conn.execute(DONE, (f"{part}:{page}", found_on))
                damaged += bad
            with conn:
                conn.execute(DONE, (part, now()))
            out(f"Common Crawl {crawl}, {host}: read ({new:,} new names so far)")
    return new, damaged


def _titles(found: list[Job]) -> set[str]:
    return {" ".join(job.title.casefold().split()) for job in found}


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
    else:
        found = download[0]
        company = found[0].company if found and board.startswith("greenhouse:") else None  # only Greenhouse says
        titles[board] = _titles(found)
        same = found_by != "cross-check" or len(titles.get(cross_of, set()) & titles[board]) >= SAME_TITLES
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
    for _ in range(2):
        rows = conn.execute("SELECT board, found_by, cross_of FROM phonebook WHERE checked_on IS NULL ORDER BY board")
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


def refresh(conn: sqlcipher3.Connection, roles: jobs.Roles, fetcher: Fetcher, out: Output = print) -> None:
    """Find new board names, check them, and record the refresh; what is done is kept if it stops midway."""
    if not any(fetcher.reachable(site) for site in sorted({collect.site_of(f"{kind}:x") for kind in BOARDS})):
        raise CollectError("No internet connection: none of the job sites could be reached. Check it and try again.")
    with conn:
        run_id = conn.execute("INSERT INTO phonebook_runs (started_at) VALUES (?)", (now(),)).lastrowid
    lines: list[str] = []
    outcome = "stopped"

    def say(line: str) -> None:
        lines.append(line)
        out(line)

    try:
        complete = True
        try:
            new, damaged = read_common_crawl(conn, fetcher, say)
            say(f"Common Crawl: {new:,} new names" + (f" ({damaged:,} damaged index lines skipped)" if damaged else ""))
        except (FetchError, SourceError) as error:
            complete = False
            say(f"Common Crawl's index could not be read ({error}). Names found so far are kept; try again later.")
        results = check_names(conn, fetcher, roles, say)
        say(
            f"Checked {results.total():,} names: {results['added']:,} added (read on every collection from now on), "
            f"{results['not found']:,} not found, {results['to confirm']:,} to confirm on the Boards page, "
            f"{results['could not be checked']:,} could not be checked (asked again next time)."
        )
        outcome = "done" if complete else "partial"
    except KeyboardInterrupt:
        outcome = "cancelled"
        raise
    finally:
        with suppress(sqlcipher3.Error), conn:  # a broken database must not hide the original error
            conn.execute(
                "UPDATE phonebook_runs SET finished_at = ?, outcome = ?, summary = ? WHERE id = ?",
                (now(), outcome, "\n".join(lines), run_id),
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.phonebook", description="The phone book of company boards.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("refresh", help="find more company boards (slow on purpose; safe to stop and run again)")
    parser.parse_args(argv)
    try:
        config = load_config()
        with (
            closing(collect.lock(config.data_dir)),  # one collection or refresh at a time
            closing(db.connect(config.data_dir)) as conn,
            closing(Fetcher()) as fetcher,
        ):
            refresh(conn, jobs.Roles.from_settings(config.roles), fetcher)
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
