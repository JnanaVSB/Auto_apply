"""Collect now, Refresh phone book and their schedules, beside the dashboard, progress kept in memory.

A collection always goes first: a refresh running when one is wanted pauses at its next request, and continues where
it stopped once the collection has finished.
"""

import logging
import sqlite3
import threading
from contextlib import ExitStack, closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import sqlcipher3

from app import collect, db, phonebook
from app.config import Config, ConfigError, load_config
from app.fetch import Fetcher, Guarded
from app.keychain import KeychainError

STOP_WAIT_SECONDS = 5  # how long closing the dashboard waits for a collection to stop at its next request
START_WAIT_SECONDS = 10  # how long "Collect now" waits to hear whether the collection got its lock
CANCELLED = "Stopped because the dashboard was closed. Everything finished before that is saved."
RESUMED = "The collection has finished; the phone-book refresh continues."
log = logging.getLogger("app.web")


@dataclass
class Collector:
    """The dashboard's collection or phone-book refresh: started by its button or its schedule, stopped when the
    dashboard closes. The two share the command's lock, so only one of them runs at a time, anywhere."""

    data_dir: Path
    fetcher: Fetcher  # the dashboard's one downloader, so every request counts towards the same per-site limit
    lines: list[str] = field(default_factory=list)  # what the command prints, line by line
    state: str = "idle"  # idle, running, done, cancelled or failed
    task: str = "collect"  # what runs, or ran last: collect or refresh
    collection_wanted: bool = False  # asked for while a refresh runs: it runs as soon as the refresh pauses
    refresh_paused: bool = False  # a collection runs inside a paused refresh
    notice: str | None = None  # why the last "Collect now" did not start; shown once on the Collect page
    _thread: threading.Thread | None = None
    _stop: threading.Event = field(default_factory=threading.Event)
    _starting: threading.Lock = field(default_factory=threading.Lock)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, task: str = "collect") -> str | None:
        """Start a collection, or with task "refresh" a phone-book refresh; returns why it could not start, or None."""
        with self._starting:
            if self.running and task == "collect" and self.task == "refresh":  # collections always go first
                if not self.collection_wanted:
                    self.collection_wanted = True
                    collect.ask_refresh_to_pause(self.data_dir)
                return None
            if self.running:
                return collect.BUSY
            try:
                config = load_config()
                if task == "collect":
                    collect.check_settings(config)
            except (ConfigError, collect.CollectError) as error:
                return str(error)
            answer = {"ready": threading.Event()}  # the collection says at once whether it got the lock
            self._thread = threading.Thread(target=self._run, args=(task, config, answer), daemon=True)
            self._thread.start()
            if not answer["ready"].wait(START_WAIT_SECONDS):  # a safety net: the collection always answers
                return "The collection did not answer in time. Reload the Collect page in a moment."
            return answer.get("refused")

    def _run(self, task: str, config: Config, answer: dict) -> None:
        def begin(*lines: str) -> None:  # the run has its lock (or waits for a refresh to pause): say so at once
            if not answer["ready"].is_set():
                self.task, self.lines, self.state = task, list(lines), "running"
                answer["ready"].set()

        try:
            with ExitStack() as held:  # the locks are taken here, since they must be released by the same thread
                try:
                    if task == "refresh":
                        held.enter_context(closing(collect.lock(self.data_dir, collect.REFRESH_LOCK)))
                        shared = collect.lock(self.data_dir)
                    else:
                        shared = collect.lock_for_collection(self.data_dir, lambda: begin(collect.PAUSING))
                except (OSError, sqlite3.Error) as error:  # e.g. a read-only data folder or a full disk
                    reason = getattr(error, "strerror", None) or error  # never a path: those include the user's name
                    raise collect.CollectError(f"The collection lock file could not be created ({reason}).") from None
                begin()
                self.fetcher.per_second = config.requests_per_second
                fetcher = Guarded(self.fetcher, self._check_stop)
                if task == "refresh":
                    self._refresh(config, fetcher, shared)
                else:
                    self._collect(config, fetcher, shared)
            self.state = "done"
        except KeyboardInterrupt:
            self.lines.append(CANCELLED)
            self.state = "cancelled"
        except (ConfigError, KeychainError, db.DatabaseError, collect.CollectError) as error:
            self._end(answer, str(error))
        except (sqlcipher3.Error, sqlite3.Error) as error:
            self._end(answer, f"the database could not be read or written ({error}).")
        except Exception as error:  # one line only; never job data or a crash report
            log.error("Unexpected error in a collection: %s", type(error).__name__)
            self._end(answer, f"something went wrong ({type(error).__name__}). Try again; the command shows more.")
        finally:
            self.collection_wanted = self.refresh_paused = False
            answer["ready"].set()

    def _collect(self, config: Config, fetcher: Guarded, shared: sqlite3.Connection) -> None:
        with closing(shared), closing(db.connect(self.data_dir)) as conn:
            collect.collect(conn, config, fetcher, self.lines.append)

    def _refresh(self, config: Config, fetcher: Guarded, shared: sqlite3.Connection) -> None:
        """A refresh that pauses for collections: one asked for here runs in this thread, one from a terminal is
        waited for; then the refresh continues where it stopped."""
        while True:
            with closing(shared), closing(db.connect(self.data_dir)) as conn:
                outcome = phonebook.refresh(conn, config, fetcher, self.lines.append)
            if self.collection_wanted:  # also when the refresh ended just before it could pause
                self.collection_wanted, self.refresh_paused, self.task = False, True, "collect"
                self._collect(config, fetcher, collect.lock_for_collection(self.data_dir))
                self.refresh_paused, self.task = False, "refresh"
                if outcome == "paused":
                    self.lines.append(RESUMED)
            if outcome != "paused":
                return
            shared = collect.wait_for_collection(self.data_dir, self._check_stop)

    def _check_stop(self) -> None:
        if self._stop.is_set():
            raise KeyboardInterrupt  # what Ctrl+C does to the command: what is finished stays saved

    def _end(self, answer: dict, message: str) -> None:
        """A run that could not start is refused at once; one that had started ends as failed."""
        if answer["ready"].is_set():
            self._failed(message)
        else:
            answer["refused"] = message

    def _failed(self, message: str) -> None:
        self.lines.append(f"Error: {message}")
        self.state = "failed"

    def stop(self) -> None:
        """Called when the dashboard closes: the collection stops before its next request."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(STOP_WAIT_SECONDS)

    def due(self, conn: sqlcipher3.Connection, every_hours: int, now: datetime | None = None) -> bool:
        """True when the schedule is on, no collection runs or is waiting (a refresh may run: it pauses), and the last
        collection started `every_hours` ago or more."""
        if not every_hours or self.collection_wanted or (self.running and self.task == "collect"):
            return False
        row = conn.execute("SELECT max(started_at) FROM collect_runs").fetchone()
        last = datetime.fromisoformat(row[0]) if row[0] else None
        return last is None or (now or datetime.now(UTC)) - last >= timedelta(hours=every_hours)

    def refresh_due(self, conn: sqlcipher3.Connection, every_days: int, now: datetime | None = None) -> bool:
        """True when the phone-book schedule is on, nothing runs here, and phonebook.due says so."""
        return bool(every_days) and not self.running and phonebook.due(conn, every_days, now or datetime.now(UTC))

    def tick(self, conn: sqlcipher3.Connection) -> None:
        """The schedules' check, once a minute while the dashboard is open; a collection goes first, a busy lock just
        waits for the next check."""
        try:
            config = load_config()
        except ConfigError:
            return  # shown on the Collect page; nothing starts until the settings are fixed
        if self.due(conn, config.collect_every_hours):
            self.start()
        elif self.refresh_due(conn, config.phonebook_every_days):
            self.start("refresh")
