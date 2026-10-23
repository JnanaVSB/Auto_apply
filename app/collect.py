"""Collect jobs: `python -m app.collect`; `add "<job link>"` saves one job, `show "<job link>"` shows one."""

import argparse
import dataclasses
import json
import sqlite3
import sys
import threading
from collections import Counter
from collections.abc import Callable
from contextlib import closing, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty, Queue
from urllib.parse import urlsplit

import sqlcipher3

from app import db, jobs
from app.config import Config, ConfigError, RoleSettings, SimplifyList, load_config
from app.fetch import ALLOWED_HOSTS, BLOCKED_HINT, Fetcher, FetchError, NotFound, Unreachable
from app.keychain import KeychainError
from app.sources import Job, SourceError, is_simplify_site, posting_of, simplify
from app.sources.boards import BOARDS

LOCK_FILE = "collect-lock.db"
BUSY = "A collection is already running. Wait for it to finish."
PROGRESS_EVERY = 50
RETIRE_AFTER = 3  # runs in a row a board answers "not found" before it is no longer read
STOPPED_HIRING_AFTER = 3  # runs in a row a board that listed jobs sends none before its company stopped hiring
SKIP_SITE_AFTER = 5  # boards in a row with no answer at all before the rest of that site is skipped this run
IN_FLIGHT = 3  # downloads open at once per site, for slow replies; new ones still start at most 1 per second
Output = Callable[[str], None]
# A source first saved here is one you set or added: a Simplify list, extra_boards, Add job or Add company.
SAVE_SOURCE = """INSERT INTO sources (name, etag, last_ok, found_by) VALUES (?, ?, ?, 'you') ON CONFLICT (name)
    DO UPDATE SET etag = excluded.etag, last_ok = excluded.last_ok, misses = 0, empty_runs = 0, retired_on = NULL"""
# A board Simplify links to is remembered; a retired one is read again once Simplify links a job posted after
# the day it was retired.
LINK_BOARD = """INSERT INTO sources (name, found_by) VALUES (?, 'simplify') ON CONFLICT (name)
    DO UPDATE SET misses = 0, retired_on = NULL WHERE retired_on IS NOT NULL AND ? > retired_on"""
LISTED_BY = "SELECT count(*) FROM job_sources WHERE source = ? AND listed = 1"
COUNT_MISS = """INSERT INTO sources (name, misses, found_by) VALUES (?, 1, 'you') ON CONFLICT (name)
    DO UPDATE SET misses = misses + 1 RETURNING misses"""  # only one of your extra_boards is new here
# Every board read on a collection (not retired, not stopped by you), and whether it is "discovered": found by the
# phone book with no job saved from it yet. Those are read after the others on their site.
BOARDS_TO_READ = """SELECT name, found_by NOT IN ('simplify', 'you')
        AND NOT EXISTS (SELECT 1 FROM job_sources WHERE source = sources.name)
    FROM sources WHERE name NOT LIKE 'simplify:%' AND retired_on IS NULL AND stopped_on IS NULL"""
COUNT_EMPTY = """INSERT INTO sources (name, empty_runs) VALUES (?, 1) ON CONFLICT (name)
    DO UPDATE SET empty_runs = empty_runs + 1 RETURNING empty_runs"""


class CollectError(Exception):
    """A problem the user can fix; the message says what to do."""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def read_board(fetcher: Fetcher, kind: str, name: str) -> tuple[list[Job], int]:
    """Download one company board and read its jobs; also return how many broken entries were skipped."""
    data, _ = fetcher.get_json(BOARDS[kind].api_url(name))
    return BOARDS[kind].parse(data, name)


def _add_up(total: jobs.Counts, counts: jobs.Counts) -> None:
    for count in dataclasses.fields(total):
        setattr(total, count.name, getattr(total, count.name) + getattr(counts, count.name))


def _plural(count: int, word: str) -> str:
    return f"{count:,} {word}{'' if count == 1 else 's'}"


def source_label(source: str) -> str:
    """A source as messages name it: Simplify Summer2027-Internships, Greenhouse board 'acme', added by you."""
    kind, _, name = source.partition(":")
    if kind == "simplify":
        return f"Simplify {name}"
    return f"{kind.title()} board '{name}'" if name else "added by you"


def source_state(listed: bool, since: str | None, reason: str | None) -> str:
    """One source's state for a job: "listed", or why it stopped (with the day the app noticed, when known)."""
    return "listed" if listed else f"{reason} (since {since})" if since else str(reason)


def _unavailable(counts: jobs.Counts) -> str:
    """E.g. "4 no longer available: 3 removed from its board, 1 company stopped hiring"."""
    reasons = ", ".join(f"{count:,} {reason}" for reason, count in counts.unavailable.most_common())
    return f"{counts.unavailable.total():,} no longer available" + (f": {reasons}" if reasons else "")


def _describe(counts: jobs.Counts) -> str:
    text = f"{counts.kept:,} matching ({counts.new:,} new, {counts.updated:,} updated, {_unavailable(counts)})"
    return text + (f", {_plural(counts.skipped, 'unusable entry')} skipped" if counts.skipped else "")


def lock(data_dir: Path, file: str = LOCK_FILE, busy: str = BUSY) -> sqlite3.Connection:
    """Hold an exclusive lock until closed; the operating system releases it if the program dies."""
    if not data_dir.is_dir():
        raise CollectError(
            "The data folder was not found. Check data_dir in data/config.yaml; "
            "for a brand-new install run: python -m app.db setup"
        )
    held = sqlite3.connect(data_dir / file, timeout=0, isolation_level=None)
    try:
        held.execute("BEGIN EXCLUSIVE")
    except sqlite3.OperationalError as error:
        held.close()
        if error.sqlite_errorcode != sqlite3.SQLITE_BUSY:
            raise
        raise CollectError(busy) from None
    return held


@dataclass
class _Run:
    """What one collection shares while it reads every source."""

    conn: sqlcipher3.Connection
    fetcher: Fetcher
    roles: jobs.Roles
    out: Output
    total: jobs.Counts = field(default_factory=jobs.Counts)
    failed: list[str] = field(default_factory=list)  # could not be downloaded
    investigate: list[str] = field(default_factory=list)  # downloaded, but the data looks wrong; nothing changed
    retired: list[str] = field(default_factory=list)
    stopped_hiring: list[str] = field(default_factory=list)
    unreachable: set[str] = field(default_factory=set)  # sites skipped for the rest of this run
    skipped: Counter = field(default_factory=Counter)  # sources skipped, per unreachable site
    silent: Counter = field(default_factory=Counter)  # per site: boards in a row with no answer at all
    _counting: threading.Lock = field(default_factory=threading.Lock)

    def check_internet(self) -> None:
        self.unreachable = {host for host in sorted(ALLOWED_HOSTS) if not self.fetcher.reachable(host)}
        if self.unreachable == ALLOWED_HOSTS:
            raise CollectError(
                "No internet connection: none of the job sites could be reached. Check your connection and try again."
            )

    def read_simplify(self, listing: SimplifyList) -> None:
        name, host = simplify.source_name(listing.url), urlsplit(listing.url).hostname
        label = source_label(name)
        if host in self.unreachable:
            self.skipped[host] += 1
            return
        row = self.conn.execute("SELECT etag FROM sources WHERE name = ?", (name,)).fetchone()
        try:
            result = self.fetcher.get_json(listing.url, row[0] if row else None)
            if result is None:
                self.out(f"{label}: unchanged since the last run")
                return
            data, etag = result
            found, skipped = simplify.parse(data, name, listing.level)
            counts = jobs.save(self.conn, name, found, self.roles, now())
        except FetchError as error:
            self.failed.append(f"{label}: {error}")
            return
        except SourceError as error:
            self.investigate.append(f"{label}: {error}")
            return
        with self.conn:  # only after a successful save, so a failed save means a fresh download next time
            self.conn.execute(SAVE_SOURCE, (name, etag, now()))
            self.conn.executemany(LINK_BOARD, simplify.linked_boards(found).items())
        counts.skipped += skipped
        _add_up(self.total, counts)
        self.out(f"{label}: {_describe(counts)}")

    def read_boards(self, extra_boards: list[str]) -> None:
        """Read every board that is not retired, plus extra_boards: one queue per site, all sites at the same time.

        Downloads run in the background, IN_FLIGHT per site; saving stays here, one board at a time, all-or-nothing.
        """
        rows = self.conn.execute(BOARDS_TO_READ).fetchall()  # boards you stopped are left out
        later = {name for name, discovered in rows if discovered} - set(extra_boards)  # yours are never "discovered"
        boards = sorted({name for name, _ in rows} | set(extra_boards), key=lambda board: (board in later, board))
        queues: dict[str, Queue] = {}
        for board in boards:
            kind, _, name = board.partition(":")
            queues.setdefault(urlsplit(BOARDS[kind].api_url(name)).hostname, Queue()).put(board)
        busiest = max((todo.qsize() for todo in queues.values()), default=0)
        minutes = _plural(busiest // 60 + 1, "minute")  # about 1 s per board on the site with the most boards
        self.out(f"Reading {_plural(len(boards), 'company board')}, all sites at the same time (about {minutes})...")
        results, stop = Queue(), threading.Event()
        for host, todo in queues.items():
            for _ in range(IN_FLIGHT):
                threading.Thread(target=self._download, args=(host, todo, results, stop), daemon=True).start()
        counts, read = jobs.Counts(), 0
        try:
            for number in range(1, len(boards) + 1):
                host, board, download = _next(results)
                if download is None:
                    self.skipped[host] += 1
                else:
                    read += self._read_board(board, download, board in extra_boards, counts) == "read"
                if number % PROGRESS_EVERY == 0:
                    self.out(f"  {number:,} of {len(boards):,} boards done")
        finally:
            stop.set()  # after an error or Ctrl+C the downloads stop before their next board
        _add_up(self.total, counts)
        self.out(f"Company boards: {read:,} of {len(boards):,} read, {_describe(counts)}")

    def _download(self, host: str, todo: Queue, results: Queue, stop: threading.Event) -> None:
        """One of a site's downloaders: takes the site's next board until none is left. Never touches the database."""
        while not stop.is_set():
            try:
                board = todo.get_nowait()
            except Empty:
                return
            if host in self.unreachable:  # the site stopped answering: the rest of it is skipped this run
                results.put((host, board, None))
                continue
            kind, _, name = board.partition(":")
            try:
                download = read_board(self.fetcher, kind, name)
            except BaseException as error:  # even a stop from the dashboard: _read_board raises it again
                download = error
            with self._counting:  # a site's downloaders share one count of boards in a row with no answer
                self.silent[host] = self.silent[host] + 1 if isinstance(download, Unreachable) else 0
                if self.silent[host] == SKIP_SITE_AFTER:  # the site went away mid-run: don't spend hours retrying it
                    self.unreachable.add(host)
            results.put((host, board, download))

    def _read_board(
        self, board: str, download: tuple[list[Job], int] | BaseException, extra: bool, counts: jobs.Counts
    ) -> str:
        """Save one downloaded board; return "read", "missing", "unreachable", "failed" or "investigate"."""
        label = source_label(board)
        try:
            if isinstance(download, BaseException):
                raise download  # a failed download, handled below exactly as before; Ctrl+C goes on up
            found, skipped = download
            reason = self._stopped_hiring(board, label) if not found and not skipped else None
            board_counts = jobs.save(self.conn, board, found, self.roles, now(), reason=reason)
        except NotFound as error:
            self.failed.append(f"{label}: {error}")
            self._count_miss(board, label, extra, counts)
            return "missing"
        except FetchError as error:
            self.failed.append(f"{label}: {error}")
            return "unreachable" if isinstance(error, Unreachable) else "failed"
        except SourceError as error:
            self.investigate.append(f"{label}: {error}")
            return "investigate"
        with self.conn:
            self.conn.execute(SAVE_SOURCE, (board, None, now()))
        board_counts.skipped += skipped
        _add_up(counts, board_counts)
        return "read"

    def _stopped_hiring(self, board: str, label: str) -> str | None:
        """An empty board that listed jobs needs investigation, until it stays empty STOPPED_HIRING_AFTER runs."""
        listed = self.conn.execute(LISTED_BY, (board,)).fetchone()[0]
        if not listed:  # a board that never had jobs, or whose jobs are already unlisted, may be empty
            return None
        with self.conn:
            empty = self.conn.execute(COUNT_EMPTY, (board,)).fetchone()[0]
        if empty < STOPPED_HIRING_AFTER:
            raise SourceError(
                f"sent no jobs, but its last good read listed {_plural(listed, 'job')} (empty {empty} of "
                f"{STOPPED_HIRING_AFTER} runs in a row; then its jobs are marked '{jobs.STOPPED_HIRING}')"
            )
        self.stopped_hiring.append(label)
        return jobs.STOPPED_HIRING

    def _count_miss(self, board: str, label: str, extra: bool, counts: jobs.Counts) -> None:
        with self.conn:
            misses = self.conn.execute(COUNT_MISS, (board,)).fetchone()[0]
            if misses >= RETIRE_AFTER and not extra:
                self.conn.execute("UPDATE sources SET retired_on = ? WHERE name = ?", (now()[:10], board))
                # in the same step: its jobs are no longer listed by it, so Simplify decides for them now
                _add_up(counts, jobs.save(self.conn, board, [], self.roles, now(), reason=jobs.BOARD_SHUT_DOWN))
                self.retired.append(label)

    def problems(self) -> list[str]:
        """Every source that failed, including one line per site that was skipped."""
        skipped = [
            f"{host}: could not be reached; {_plural(count, 'source')} skipped this run, closing nothing "
            f"({BLOCKED_HINT})"
            for host, count in sorted(self.skipped.items())
        ]
        return self.failed + skipped

    def summary(self) -> None:
        total, problems = self.total, self.problems()
        self.out(f"Total: {total.new:,} new, {total.updated:,} updated, {_unavailable(total)}.")
        if not problems:
            self.out("No sources failed.")
        sections = (
            (problems, f"{_plural(len(problems), 'source')} failed:"),
            (
                self.investigate,
                f"Needs investigation ({_plural(len(self.investigate), 'source')}; the data looks different from "
                "usual, so nothing was changed for them):",
            ),
            (
                self.retired,
                f"Stopped reading {_plural(len(self.retired), 'board')} not found {RETIRE_AFTER} runs in a row; "
                f"their jobs are marked '{jobs.BOARD_SHUT_DOWN}' and Simplify now decides for any it also lists "
                "(read again if Simplify links a newer job there, or if you add them to extra_boards):",
            ),
            (
                self.stopped_hiring,
                f"Company stopped hiring ({_plural(len(self.stopped_hiring), 'board')} empty "
                f"{STOPPED_HIRING_AFTER} runs in a row; their jobs are kept and marked '{jobs.STOPPED_HIRING}'):",
            ),
        )
        for lines, heading in sections:
            if lines:
                self.out(heading)
                for line in lines:
                    self.out(f"  - {line}")


def _next(results: Queue) -> tuple:
    """The next finished download; waits in short steps so Ctrl+C is noticed on every system (Windows too)."""
    while True:
        with suppress(Empty):
            return results.get(timeout=1)


def collect(conn: sqlcipher3.Connection, config: Config, fetcher: Fetcher, out: Output = print) -> None:
    """Read Simplify's lists, then every board with all sites at the same time; save each source all-or-nothing."""
    run = _Run(conn, fetcher, jobs.Roles.from_settings(config.roles), out)
    run.check_internet()
    with conn:
        run_id = conn.execute("INSERT INTO collect_runs (started_at) VALUES (?)", (now(),)).lastrowid
    outcome = "stopped"
    try:
        for listing in config.sources.simplify_lists:
            run.read_simplify(listing)
        run.read_boards(config.sources.extra_boards)
        outcome = "done"
    except KeyboardInterrupt:
        outcome = "cancelled"
        raise
    finally:
        failures = run.problems() + [f"needs investigation: {line}" for line in run.investigate]
        unavailable = run.total.unavailable.total()
        with suppress(sqlcipher3.Error), conn:  # a broken database must not hide the original error
            conn.execute(
                "UPDATE collect_runs SET finished_at = ?, outcome = ?, new_jobs = ?, updated_jobs = ?, "
                "closed_jobs = ?, failures = ? WHERE id = ?",
                (now(), outcome, run.total.new, run.total.updated, unavailable, json.dumps(failures), run_id),
            )
    run.summary()


def add(
    conn: sqlcipher3.Connection,
    fetcher: Fetcher,
    link: str,
    ask: Callable[[str], str] = input,
    details: tuple[str, str] | None = None,
) -> str:
    """Save one job the user found; their role and US filters do not apply. Returns what happened.

    For a link that is not on a job board, `details` gives the company and job title; without them, `ask` asks.
    """
    link = link.strip()
    parts = urlsplit(link)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise CollectError("That is not a web link; it must start with https://")
    if is_simplify_site(link):
        raise CollectError(
            "This is a simplify.jobs link, which the app must not open. "
            "Open it in your browser, click Apply, and add the company's own link instead."
        )
    posting = posting_of(link)
    if posting:
        kind, board, job_id = posting
        source = f"{kind}:{board}"
        job = next((job for job in read_board(fetcher, kind, board)[0] if job.source_job_id == job_id), None)
        if job is None:
            raise CollectError(f"That job is no longer on the {kind.title()} board '{board}'; it may be closed.")
    else:  # another site: stored with its link only; the description is read in a later phase
        source = "manual"
        job = Job(source=source, source_job_id=link, company="", title="", url=link, locations=[])
        row = conn.execute("SELECT company, title FROM jobs WHERE apply_url = ?", (jobs.clean_link(job),)).fetchone()
        if row:
            return f"Already saved: {row[0]}, {row[1]}"
        company, title = details or (ask("Company: "), ask("Job title: "))
        job.company, job.title = company.strip(), title.strip()
        if not job.company or not job.title:
            raise CollectError("Both the company and the job title are needed.")
    counts = jobs.save(
        conn, source, [job], jobs.Roles.from_settings(RoleSettings()), now(), complete=False, chosen=True
    )
    if counts.skipped:
        raise CollectError("That link cannot be used; copy the job's address straight from the browser.")
    if posting:
        with conn:  # remembered (and no longer retired), so later collections keep this job up to date
            conn.execute(SAVE_SOURCE, (source, None, now()))
    return f"{'Added' if counts.new else 'Already saved'}: {job.company}, {job.title}"


def show(conn: sqlcipher3.Connection, link: str) -> str:
    """One saved job's status, with each source's own state. Reads only."""
    sql = "SELECT id, company, title, status, status_reason FROM jobs WHERE apply_url = ?"
    job = conn.execute(sql, (jobs.clean_url(link),)).fetchone()
    if job is None:
        raise CollectError("No saved job has that link. Copy the link exactly as it was added or collected.")
    job_id, company, title, status, reason = job
    lines = [f"{company}, {title}", f"Status: {status}" + (f" ({reason})" if reason else ""), "Sources:"]
    entries = conn.execute(
        "SELECT source, listed, unlisted_on, unlisted_reason FROM job_sources WHERE job_id = ? ORDER BY rowid",
        (job_id,),
    )
    for source, listed, since, why in entries:
        lines.append(f"  - {source_label(source)}: {source_state(listed, since, why)}")
    return "\n".join(lines)


def check_settings(config: Config) -> None:
    if not (config.roles.simplify_categories or config.roles.keywords):
        raise CollectError("No roles set in data/config.yaml. Copy the 'roles' section from config.example.yaml.")
    if not (config.sources.simplify_lists or config.sources.extra_boards):
        raise CollectError("No sources set in data/config.yaml. Copy the 'sources' section from config.example.yaml.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.collect", description="Collect matching jobs.")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("add", help="save one job by its link").add_argument("link")
    commands.add_parser("show", help="show one saved job's status and sources").add_argument("link")
    args = parser.parse_args(argv)
    try:
        config = load_config()
        if args.command is None:
            check_settings(config)
        with (
            closing(lock(config.data_dir)),  # first, so two copies can never both upgrade the database
            closing(db.connect(config.data_dir)) as conn,
            closing(Fetcher()) as fetcher,
        ):
            if args.command == "add":
                print(add(conn, fetcher, args.link))
            elif args.command == "show":
                print(show(conn, args.link))
            else:
                collect(conn, config, fetcher)
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
    except (FetchError, SourceError) as error:  # only `add` lets these through; `collect` lists them per source
        print(f"Error: the job's board could not be read ({error}).", file=sys.stderr)
    except (EOFError, KeyboardInterrupt):
        saved = " Sources finished before you stopped are saved; run it again to continue." if not args.command else ""
        print(f"\nCancelled.{saved}", file=sys.stderr)
    else:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
