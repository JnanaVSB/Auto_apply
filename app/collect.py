"""Collect jobs: `python -m app.collect`, or `python -m app.collect add "<job link>"` for one job."""

import argparse
import dataclasses
import json
import sqlite3
import sys
from collections.abc import Callable
from contextlib import closing, suppress
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import sqlcipher3

from app import db, jobs
from app.config import Config, ConfigError, SimplifyList, load_config
from app.fetch import Fetcher, FetchError
from app.keychain import KeychainError
from app.sources import Job, SourceError, ashby, greenhouse, lever, posting_of, simplify

BOARDS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}
LOCK_FILE = "collect-lock.db"
PROGRESS_EVERY = 50
Output = Callable[[str], None]
SAVE_SOURCE = """INSERT INTO sources (name, etag, last_ok) VALUES (?, ?, ?)
    ON CONFLICT (name) DO UPDATE SET etag = excluded.etag, last_ok = excluded.last_ok"""


class CollectError(Exception):
    """A problem the user can fix; the message says what to do."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _add_up(total: jobs.Counts, counts: jobs.Counts) -> None:
    for field in dataclasses.fields(total):
        setattr(total, field.name, getattr(total, field.name) + getattr(counts, field.name))


def _describe(counts: jobs.Counts) -> str:
    text = f"{counts.kept:,} matching ({counts.new:,} new, {counts.updated:,} updated, {counts.closed:,} closed)"
    return text + (f", {counts.skipped:,} unusable entries skipped" if counts.skipped else "")


def _lock(data_dir: Path) -> sqlite3.Connection:
    """Hold an exclusive lock for the whole run; the operating system releases it if the program dies."""
    lock = sqlite3.connect(data_dir / LOCK_FILE, timeout=0, isolation_level=None)
    try:
        lock.execute("BEGIN EXCLUSIVE")
    except sqlite3.OperationalError as error:
        lock.close()
        if error.sqlite_errorcode != sqlite3.SQLITE_BUSY:
            raise
        raise CollectError("A collection is already running. Wait for it to finish.") from None
    return lock


def collect(conn: sqlcipher3.Connection, config: Config, fetcher: Fetcher, out: Output = print) -> None:
    """Read every source one at a time, saving each all-or-nothing, then print a summary."""
    roles = jobs.Roles.from_settings(config.roles.simplify_categories, config.roles.keywords)
    with conn:
        run_id = conn.execute("INSERT INTO collect_runs (started_at) VALUES (?)", (_now(),)).lastrowid
    total, failures, outcome = jobs.Counts(), [], "stopped"
    try:
        for listing in config.sources.simplify_lists:
            _read_simplify(conn, fetcher, listing, roles, total, failures, out)
        _read_boards(conn, fetcher, config.sources.extra_boards, roles, total, failures, out)
        outcome = "done"
    except KeyboardInterrupt:
        outcome = "cancelled"
        raise
    finally:
        with suppress(sqlcipher3.Error), conn:  # a broken database must not hide the original error
            conn.execute(
                "UPDATE collect_runs SET finished_at = ?, outcome = ?, new_jobs = ?, updated_jobs = ?, "
                "closed_jobs = ?, failures = ? WHERE id = ?",
                (_now(), outcome, total.new, total.updated, total.closed, json.dumps(failures), run_id),
            )
    out(f"Total: {total.new:,} new, {total.updated:,} updated, {total.closed:,} closed.")
    out(f"{len(failures)} sources failed:" if failures else "No sources failed.")
    for failure in failures:
        out(f"  - {failure}")


def _read_simplify(
    conn: sqlcipher3.Connection,
    fetcher: Fetcher,
    listing: SimplifyList,
    roles: jobs.Roles,
    total: jobs.Counts,
    failures: list[str],
    out: Output,
) -> None:
    name = simplify.source_name(listing.url)
    label = name.replace("simplify:", "Simplify ")
    row = conn.execute("SELECT etag FROM sources WHERE name = ?", (name,)).fetchone()
    try:
        result = fetcher.get_json(listing.url, row[0] if row else None)
        if result is None:
            out(f"{label}: unchanged since the last run")
            return
        data, etag = result
        found, skipped = simplify.parse(data, name, listing.level)
        counts = jobs.save(conn, name, found, roles, _now())
    except (FetchError, SourceError) as error:
        failures.append(f"{label}: {error}")
        return
    with conn:  # only after a successful save, so a failed save means a fresh download next time
        conn.execute(SAVE_SOURCE, (name, etag, _now()))
        conn.executemany(
            "INSERT OR IGNORE INTO sources (name) VALUES (?)", [(b,) for b in simplify.linked_boards(found)]
        )
    counts.skipped += skipped
    _add_up(total, counts)
    out(f"{label}: {_describe(counts)}")


def _read_boards(
    conn: sqlcipher3.Connection,
    fetcher: Fetcher,
    extra_boards: list[str],
    roles: jobs.Roles,
    total: jobs.Counts,
    failures: list[str],
    out: Output,
) -> None:
    known = {name for (name,) in conn.execute("SELECT name FROM sources WHERE name NOT LIKE 'simplify:%'")}
    boards = sorted(known | set(extra_boards))  # every board ever found keeps being read
    out(f"Reading {len(boards):,} company boards one at a time (about {len(boards) * 2 // 60 + 1} minutes)...")
    board_counts, read = jobs.Counts(), 0
    for number, board in enumerate(boards, 1):
        kind, _, name = board.partition(":")
        try:
            data, _ = fetcher.get_json(BOARDS[kind].api_url(name))
            found, skipped = BOARDS[kind].parse(data, name)
            counts = jobs.save(conn, board, found, roles, _now())
        except (FetchError, SourceError) as error:
            failures.append(f"{kind.title()} board '{name}': {error}")
        else:
            with conn:
                conn.execute(SAVE_SOURCE, (board, None, _now()))
            counts.skipped += skipped
            _add_up(board_counts, counts)
            read += 1
        if number % PROGRESS_EVERY == 0:
            out(f"  {number:,} of {len(boards):,} boards done")
    _add_up(total, board_counts)
    out(f"Company boards: {read:,} of {len(boards):,} read, {_describe(board_counts)}")


def add(conn: sqlcipher3.Connection, fetcher: Fetcher, link: str, ask: Callable[[str], str] = input) -> str:
    """Save one job the user found; their role and US filters do not apply. Returns what happened."""
    link = link.strip()
    parts = urlsplit(link)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise CollectError("That is not a web link; it must start with https://")
    if parts.hostname == "simplify.jobs" or parts.hostname.endswith(".simplify.jobs"):
        raise CollectError(
            "This is a simplify.jobs link, which the app must not open. "
            "Open it in your browser, click Apply, and add the company's own link instead."
        )
    posting = posting_of(link)
    if posting:
        kind, board, job_id = posting
        source = f"{kind}:{board}"
        data, _ = fetcher.get_json(BOARDS[kind].api_url(board))
        job = next((job for job in BOARDS[kind].parse(data, board)[0] if job.source_job_id == job_id), None)
        if job is None:
            raise CollectError(f"That job is no longer on the {kind.title()} board '{board}'; it may be closed.")
    else:  # another site: stored with its link only; the description is read in a later phase
        source = "manual"
        job = Job(source=source, source_job_id=link, company="", title="", url=link, locations=[])
        row = conn.execute("SELECT company, title FROM jobs WHERE apply_url = ?", (jobs.clean_link(job),)).fetchone()
        if row:
            return f"Already saved: {row[0]}, {row[1]}"
        job.company, job.title = ask("Company: ").strip(), ask("Job title: ").strip()
        if not job.company or not job.title:
            raise CollectError("Both the company and the job title are needed.")
    counts = jobs.save(conn, source, [job], jobs.Roles.from_settings([], []), _now(), complete=False, chosen=True)
    if counts.skipped:
        raise CollectError("That link cannot be used; copy the job's address straight from the browser.")
    if posting:
        with conn:  # remembered, so every later collection keeps this job up to date
            conn.execute("INSERT OR IGNORE INTO sources (name) VALUES (?)", (source,))
    return f"{'Added' if counts.new else 'Already saved'}: {job.company}, {job.title}"


def _check_settings(config: Config) -> None:
    if not (config.roles.simplify_categories or config.roles.keywords):
        raise CollectError("No roles set in data/config.yaml. Copy the 'roles' section from config.example.yaml.")
    if not (config.sources.simplify_lists or config.sources.extra_boards):
        raise CollectError("No sources set in data/config.yaml. Copy the 'sources' section from config.example.yaml.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.collect", description="Collect matching jobs.")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("add", help="save one job by its link").add_argument("link")
    args = parser.parse_args(argv)
    try:
        config = load_config()
        if args.command is None:
            _check_settings(config)
        with (
            closing(db.connect(config.data_dir)) as conn,
            closing(_lock(config.data_dir)),
            closing(Fetcher()) as fetcher,
        ):
            if args.command == "add":
                print(add(conn, fetcher, args.link))
            else:
                collect(conn, config, fetcher)
    except (ConfigError, KeychainError, db.DatabaseError, CollectError) as error:
        print(f"Error: {error}", file=sys.stderr)
    except (FetchError, SourceError) as error:  # only `add` lets these through; `collect` lists them per source
        print(f"Error: the job's board could not be read ({error}).", file=sys.stderr)
    except (sqlcipher3.Error, sqlite3.Error) as error:
        print(f"Error: the database could not be read or written ({error}).", file=sys.stderr)
    except OSError as error:  # only the file name, since full paths include the user's name
        where = f": {Path(error.filename).name}" if error.filename else ""
        print(f"Error: {error.strerror or error}{where}", file=sys.stderr)
    except (EOFError, KeyboardInterrupt):
        saved = " Sources finished before you stopped are saved; run it again to continue." if not args.command else ""
        print(f"\nCancelled.{saved}", file=sys.stderr)
    else:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
