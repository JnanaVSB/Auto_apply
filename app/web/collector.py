"""Collect now, Refresh phone book and their schedules: one at a time, beside the dashboard, progress kept in memory."""

import logging
import sqlite3
import threading
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import sqlcipher3

from app import collect, db, phonebook
from app.config import Config, ConfigError, load_config
from app.fetch import Fetcher
from app.keychain import KeychainError

STOP_WAIT_SECONDS = 5  # how long closing the dashboard waits for a collection to stop at its next request
START_WAIT_SECONDS = 10  # how long "Collect now" waits to hear whether the collection got its lock
CANCELLED = "Stopped because the dashboard was closed. Everything finished before that is saved."
log = logging.getLogger("app.web")


class _Stoppable:
    """The downloader, refusing to start another request once the dashboard is closing."""

    def __init__(self, fetcher: Fetcher, stop: threading.Event) -> None:
        self._fetcher, self._stop = fetcher, stop
        self.per_second = fetcher.per_second

    def _check(self) -> None:
        if self._stop.is_set():
            raise KeyboardInterrupt  # what Ctrl+C does to the command: finished sources stay saved

    def reachable(self, host: str) -> bool:
        self._check()
        return self._fetcher.reachable(host)

    def get_json(self, url: str, etag: str | None = None):
        self._check()
        return self._fetcher.get_json(url, etag)

    def get_text(self, url: str) -> str:
        self._check()
        return self._fetcher.get_text(url)


@dataclass
class Collector:
    """The dashboard's collection or phone-book refresh: started by its button or its schedule, stopped when the
    dashboard closes. The two share the command's lock, so only one of them runs at a time, anywhere."""

    data_dir: Path
    new_fetcher: Callable[[], Fetcher] = Fetcher
    lines: list[str] = field(default_factory=list)  # what the command prints, line by line
    state: str = "idle"  # idle, running, done, cancelled or failed
    task: str = "collect"  # what runs, or ran last: collect or refresh
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
        lock = None
        try:  # taken here, since the lock must be released by the same thread that took it
            lock = collect.lock(self.data_dir)  # the command's own lock: one collection at a time, anywhere
        except collect.CollectError as error:
            answer["refused"] = str(error)
        except Exception as error:  # e.g. a read-only data folder or a full disk
            reason = getattr(error, "strerror", None) or error  # never a path: those include the user's name
            answer["refused"] = f"The collection lock file could not be created ({reason})."
        finally:  # whatever happened, "Collect now" is told at once
            if lock is not None:
                self.task, self.lines, self.state = task, [], "running"
            answer["ready"].set()
        if lock is None:
            return
        try:
            with closing(lock), closing(db.connect(self.data_dir)) as conn, closing(self.new_fetcher()) as fetcher:
                fetcher.per_second = config.requests_per_second
                work = phonebook.refresh if task == "refresh" else collect.collect
                work(conn, config, _Stoppable(fetcher, self._stop), self.lines.append)
            self.state = "done"
        except KeyboardInterrupt:
            self.lines.append(CANCELLED)
            self.state = "cancelled"
        except (ConfigError, KeychainError, db.DatabaseError, collect.CollectError) as error:
            self._failed(str(error))
        except (sqlcipher3.Error, sqlite3.Error) as error:
            self._failed(f"the database could not be read or written ({error}).")
        except Exception as error:  # one line only; never job data or a crash report
            log.error("Unexpected error in a collection: %s", type(error).__name__)
            self._failed(f"something went wrong ({type(error).__name__}). Try again; the command shows more.")

    def _failed(self, message: str) -> None:
        self.lines.append(f"Error: {message}")
        self.state = "failed"

    def stop(self) -> None:
        """Called when the dashboard closes: the collection stops before its next request."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(STOP_WAIT_SECONDS)

    def due(self, conn: sqlcipher3.Connection, every_hours: int, now: datetime | None = None) -> bool:
        """True when the schedule is on, nothing runs, and the last collection started `every_hours` ago or more."""
        if not every_hours or self.running:
            return False
        row = conn.execute("SELECT max(started_at) FROM collect_runs").fetchone()
        last = datetime.fromisoformat(row[0]) if row[0] else None
        return last is None or (now or datetime.now(UTC)) - last >= timedelta(hours=every_hours)

    def refresh_due(self, conn: sqlcipher3.Connection, every_days: int, now: datetime | None = None) -> bool:
        """True when the phone-book schedule is on, nothing runs, the last complete refresh started `every_days` ago or
        more (or none ever finished), and no refresh started in the last day: an unfinished one is retried daily."""
        if not every_days or self.running:
            return False
        done, last = conn.execute(
            "SELECT max(CASE WHEN outcome = 'done' THEN started_at END), max(started_at) FROM phonebook_runs"
        ).fetchone()
        now = now or datetime.now(UTC)

        def ago(started: str | None, wait: timedelta) -> bool:
            return started is None or now - datetime.fromisoformat(started) >= wait

        return ago(done, timedelta(days=every_days)) and ago(last, timedelta(days=1))

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
