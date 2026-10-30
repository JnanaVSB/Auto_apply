"""The Writer beside the dashboard: Prepare starts it for one job, in the background. One job at a time, under its own
lock so a second dashboard can't run it too; jobs pressed while it is busy wait their turn here (forgotten if the
dashboard closes: they show Prepare again). Each job is saved step by step as it goes (app/council/runs.py)."""

import logging
import threading
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import sqlcipher3

from app import collect, db
from app.config import ConfigError, load_config
from app.council import runs

PREPARE_LOCK = "prepare-lock.db"
BUSY = "The Writer is working in another dashboard or terminal: press Prepare again when it has finished."
log = logging.getLogger("app.web")


@dataclass
class Preparer:
    data_dir: Path
    waiting: list[tuple[int, str]] = field(default_factory=list)  # (job, your feedback or ""), first pressed first
    current: int | None = None  # the job being written now
    notice: str | None = None  # why the last press could not run; shown in the job's box
    _thread: threading.Thread | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def start(self, job_id: int, feedback: str = "") -> None:
        """You pressed Prepare (or sent your changes): the job is written now, or after the jobs before it. A second
        press while it waits or is written changes nothing."""
        with self._lock:
            if self._place(job_id) is not None:
                return
            self.waiting.append((job_id, feedback))
            self.notice = None
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()

    def place(self, job_id: int) -> int | None:
        """0 while the job is being written, 1 or more while it waits (how many go first), else None."""
        with self._lock:
            return self._place(job_id)

    def _place(self, job_id: int) -> int | None:
        if job_id == self.current:
            return 0
        waiting = [job for job, _ in self.waiting]
        return waiting.index(job_id) + 1 if job_id in waiting else None

    def recover(self) -> None:
        """At the dashboard's start: calls an earlier dashboard left waiting are marked interrupted
        (app/council/runs.py), unless a Writer is running elsewhere right now."""
        try:
            with closing(collect.lock(self.data_dir, PREPARE_LOCK, BUSY)), closing(db.connect(self.data_dir)) as conn:
                runs.recover(conn)
        except collect.CollectError:
            pass  # a Writer runs elsewhere: it recovers when it starts its next job

    def _run(self) -> None:
        while (job := self._next()) is not None:
            try:  # the lock for each job, so it is never held while waiting for a press
                with (
                    closing(collect.lock(self.data_dir, PREPARE_LOCK, BUSY)),
                    closing(db.connect(self.data_dir)) as conn,
                ):
                    runs.recover(conn)
                    runs.prepare(conn, load_config(), *job)
            except collect.CollectError as error:
                self._stop(str(error))
            except ConfigError as error:
                self._stop(f"The settings can't be read: {error}")
            except (db.DatabaseError, sqlcipher3.Error, OSError) as error:
                self._stop(db.error_line(error))
            except Exception as error:  # one line only; never job data, your text or a crash report
                log.error("Unexpected error in the Writer: %s", type(error).__name__)

    def _next(self) -> tuple[int, str] | None:
        with self._lock:
            job = self.waiting.pop(0) if self.waiting else None
            self.current = job and job[0]
            if job is None:
                self._thread = None  # a press from now on starts a new run
            return job

    def _stop(self, notice: str) -> None:
        """Nothing more can run now: the waiting jobs are let go, and the box says why."""
        with self._lock:
            self.waiting.clear()
            self.notice = notice
