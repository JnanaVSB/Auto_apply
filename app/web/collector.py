"""Collect now and the schedule: one collection at a time, beside the dashboard, with its progress kept in memory."""

import logging
import sqlite3
import threading
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import sqlcipher3

from app import collect, db
from app.config import Config, ConfigError, load_config
from app.fetch import Fetcher
from app.keychain import KeychainError

STOP_WAIT_SECONDS = 5  # how long closing the dashboard waits for a collection to stop at its next request
CANCELLED = "Stopped because the dashboard was closed. Sources finished before that are saved."
log = logging.getLogger("app.web")


class _Stoppable:
    """The downloader, refusing to start another request once the dashboard is closing."""

    def __init__(self, fetcher: Fetcher, stop: threading.Event) -> None:
        self._fetcher, self._stop = fetcher, stop

    def _check(self) -> None:
        if self._stop.is_set():
            raise KeyboardInterrupt  # what Ctrl+C does to the command: finished sources stay saved

    def reachable(self, host: str) -> bool:
        self._check()
        return self._fetcher.reachable(host)

    def get_json(self, url: str, etag: str | None = None):
        self._check()
        return self._fetcher.get_json(url, etag)


@dataclass
class Collector:
    """The dashboard's collection: started by "Collect now" or the schedule, stopped when the dashboard closes."""

    data_dir: Path
    new_fetcher: Callable[[], Fetcher] = Fetcher
    lines: list[str] = field(default_factory=list)  # what the command prints, line by line
    state: str = "idle"  # idle, running, done, cancelled or failed
    _thread: threading.Thread | None = None
    _stop: threading.Event = field(default_factory=threading.Event)
    _starting: threading.Lock = field(default_factory=threading.Lock)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> str | None:
        """Start a collection; returns why it could not start, or None."""
        with self._starting:
            if self.running:
                return collect.BUSY
            try:
                config = load_config()
                collect._check_settings(config)
            except (ConfigError, collect.CollectError) as error:
                return str(error)
            answer = {"ready": threading.Event()}  # the collection says at once whether it got the lock
            self._thread = threading.Thread(target=self._run, args=(config, answer), daemon=True)
            self._thread.start()
            answer["ready"].wait()
            return answer.get("refused")

    def _run(self, config: Config, answer: dict) -> None:
        try:  # taken here, since the lock must be released by the same thread that took it
            lock = collect._lock(self.data_dir)  # the command's own lock: one collection at a time, anywhere
        except collect.CollectError as error:
            answer["refused"] = str(error)
            answer["ready"].set()
            return
        self.lines, self.state = [], "running"
        answer["ready"].set()
        try:
            with closing(lock), closing(db.connect(self.data_dir)) as conn, closing(self.new_fetcher()) as fetcher:
                collect.collect(conn, config, _Stoppable(fetcher, self._stop), self.lines.append)
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

    def tick(self, conn: sqlcipher3.Connection) -> None:
        """The schedule's check, once a minute while the dashboard is open; a busy lock just waits for the next one."""
        try:
            every = load_config().collect_every_hours
        except ConfigError:
            return  # shown on the Collect page; nothing starts until the settings are fixed
        if self.due(conn, every):
            self.start()
