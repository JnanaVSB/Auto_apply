"""The export beside the dashboard (app/export): a run when you press Export or when it is due, in a thread of its own
with its own connection, under its own lock, so a second dashboard never writes the same folder. A run stopped by
closing the dashboard loses nothing: the next one carries on. Problem lines name applications: they stay in memory."""

import logging
import threading
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import sqlcipher3

from app import collect, db, export
from app.config import Config, ConfigError, load_config
from app.export import papers

LOCK = "export-lock.db"
BUSY = "An export is running"
UNEXPECTED = "The export stopped on an unexpected error; the next export carries on from where it stopped."
TRIED = """INSERT INTO export_runs (id, tried_on) VALUES (1, ?)
    ON CONFLICT (id) DO UPDATE SET tried_on = excluded.tried_on"""
ENDED = "UPDATE export_runs SET last_result = ?, good_on = coalesce(?, good_on) WHERE id = 1"
log = logging.getLogger("app.web")


@dataclass
class Exporter:
    data_dir: Path
    done: int = 0  # the run in progress, for the status line
    total: int = 0
    notice: str | None = None  # the last run's words
    problems: list[str] = field(default_factory=list)  # the last run's lines, in memory only
    tried: datetime | None = None  # the last run's start: a second dashboard's refusal isn't saved
    _thread: threading.Thread | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def running(self) -> bool:
        return self._thread is not None

    def start(self, start: date | None, end: date | None) -> str | None:
        """An Export press: BUSY while this dashboard runs one, else None, and the thread is going."""
        return self._begin((start, end))

    def tick(self, conn: sqlcipher3.Connection) -> None:
        """Part of the once-a-minute check, on the request loop: reads, and at most starts the thread."""
        try:
            settings = load_config().export
        except ConfigError:  # shown on the Settings pages
            return
        if not settings.every_days or not settings.folder:
            return
        row = conn.execute("SELECT tried_on FROM export_runs WHERE id = 1").fetchone()
        saved = datetime.fromisoformat(row[0]) if row and row[0] else None
        last = max(filter(None, (saved, self.tried)), default=None)
        if last is None or datetime.now(UTC) - last >= timedelta(days=settings.every_days):
            self._begin(None)

    def _begin(self, press: tuple[date | None, date | None] | None) -> str | None:
        with self._lock:
            if self._thread is not None:
                return BUSY
            self.done, self.total, self.problems = 0, 0, []
            self._thread = threading.Thread(target=self._run, args=(press,), daemon=True)
            self._thread.start()
        return None

    def _run(self, press: tuple[date | None, date | None] | None) -> None:
        try:
            self.notice = self._words(press)
        finally:
            with self._lock:
                self._thread = None

    def _words(self, press: tuple[date | None, date | None] | None) -> str:
        self.tried = datetime.now(UTC)
        try:
            with closing(collect.lock(self.data_dir, LOCK, BUSY)), closing(db.connect(self.data_dir)) as conn:
                config = load_config()
                with conn:
                    conn.execute(TRIED, (collect.now(),))
                return self._export(conn, config, press)
        except collect.CollectError as error:
            return str(error)
        except ConfigError as error:
            return f"The settings can't be read: {error}"
        except (db.DatabaseError, sqlcipher3.Error, OSError) as error:
            return db.error_line(error)
        except Exception as error:  # the type only: its text could hold a job's words
            log.error("Unexpected error in the export: %s", type(error).__name__)
            return UNEXPECTED

    def _export(
        self, conn: sqlcipher3.Connection, config: Config, press: tuple[date | None, date | None] | None
    ) -> str:
        start, end = press or (_since(conn, config.export.every_days), None)
        result = export.run(conn, self.data_dir, config.export.folder, start, end, press is None, self._step)
        self.problems = result.problems
        words = (
            result.stopped if result.selected is None else ". ".join(filter(None, (result.summary(), result.stopped)))
        )
        with conn:
            conn.execute(ENDED, (words, None if result.stopped else collect.now()))
        return words

    def _step(self, done: int, total: int) -> None:
        self.done, self.total = done, total


def _since(conn: sqlcipher3.Connection, every_days: int) -> date:
    """A schedule's first date: the set days back, or the last good run if that is earlier, so a dashboard closed for
    three weeks loses nothing."""
    back = date.today() - timedelta(days=every_days)
    row = conn.execute("SELECT good_on FROM export_runs WHERE id = 1").fetchone()
    return min(back, date.fromisoformat(papers.day(row[0]))) if row and row[0] else back
