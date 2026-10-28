"""Scoring beside the dashboard: Score now, Score these, the answer key's model comparison, and automatic scoring
from the once-a-minute check. Progress is kept in memory; results are saved job by job, as in the terminal."""

import logging
import threading
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import sqlcipher3

from app import collect, db, llm, score
from app.config import Config, ConfigError, load_config
from app.fetch import Fetcher
from app.keychain import KeychainError
from app.resume import ResumeError, read_resume
from app.scoring import files, queue, stage_a

STOP_WAIT_SECONDS = 5  # how long closing the dashboard waits for scoring to stop after its current job
RETRY_AFTER = timedelta(minutes=30)  # automatic scoring waits this long after a failed run
STOPPED = "Stopped because the dashboard was closed. Every job scored before that is saved."
log = logging.getLogger("app.web")


@dataclass
class Scorer:
    data_dir: Path
    fetcher: Fetcher  # the dashboard's one downloader, for the scoring files
    lines: list[str] = field(default_factory=list)  # what the command prints, line by line
    state: str = "idle"  # idle, running, done, stopped or failed
    failed_at: datetime | None = None  # when the last run failed: automatic scoring waits RETRY_AFTER
    notice: str | None = None  # why the last request did not start; shown once on the Collect page
    _thread: threading.Thread | None = None
    _stop: threading.Event = field(default_factory=threading.Event)
    _starting: threading.Lock = field(default_factory=threading.Lock)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, job_ids: list[int] | None = None, model: str | None = None) -> str | None:
        """Score the waiting jobs, or just `job_ids` (with `model` instead of the chosen one, to compare models);
        returns why it could not start, or None."""
        with self._starting:
            if self.running:
                return score.BUSY
            self.lines, self.state = [], "running"
            self._thread = threading.Thread(target=self._run, args=(job_ids, model), daemon=True)
            self._thread.start()
        return None

    def _run(self, job_ids: list[int] | None, model: str | None) -> None:
        try:
            with closing(collect.lock(self.data_dir, score.SCORE_LOCK, score.BUSY)):
                config = _with_model(load_config(), model)
                prepared = score.prepare(config, self.fetcher, self.lines.append)
                with closing(db.connect(self.data_dir)) as conn:  # opened by the dashboard already: no upgrade
                    queue.run(conn, config, *prepared, job_ids=job_ids, stop=self._check_stop, out=self.lines.append)
            self.state, self.failed_at = "done", None
        except KeyboardInterrupt:
            self.lines.append(STOPPED)
            self.state = "stopped"
        except (
            ConfigError, KeychainError, db.DatabaseError, collect.CollectError, files.SetupError, ResumeError,
            llm.StopScoring, sqlcipher3.Error, OSError,
        ) as error:  # fmt: skip
            self._failed(db.error_line(error))
        except Exception as error:  # one line only; never job data or a crash report
            log.error("Unexpected error in scoring: %s", type(error).__name__)
            self._failed(f"Error: something went wrong ({type(error).__name__}). The terminal command shows more.")

    def _failed(self, line: str) -> None:
        self.lines.append(line)
        self.state, self.failed_at = "failed", datetime.now(UTC)

    def _check_stop(self) -> None:
        if self._stop.is_set():
            raise KeyboardInterrupt  # what Ctrl+C does to the command: what is finished stays saved

    def stop(self) -> None:
        """Called when the dashboard closes: scoring stops after its current job."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(STOP_WAIT_SECONDS)

    def due(self, conn: sqlcipher3.Connection, config: Config, now: datetime | None = None) -> bool:
        """True when automatic scoring is on, nothing scores yet, no collection runs, today's time is not used up,
        the last failure is RETRY_AFTER ago, and there is something to do."""
        settings, now = config.scoring, now or datetime.now(UTC)
        if not settings.automatic or self.running or (self.failed_at and now - self.failed_at < RETRY_AFTER):
            return False
        if queue.collection_running(conn) or queue.used_today(conn) >= settings.minutes_per_day * 60:
            return False
        try:
            resume = read_resume(config.data_dir)
        except ResumeError:
            return False  # nothing can be scored; the Collect page shows why after a Score now
        keywords, extra = config.roles.keywords, config.scoring.extra_skills
        setup = stage_a.setup_fingerprint(resume, keywords, extra)
        return queue.has_work(conn, config.ai.evaluator.model, setup, resume.fingerprint)

    def tick(self, conn: sqlcipher3.Connection) -> None:
        """Part of the once-a-minute check while the dashboard is open."""
        try:
            config = load_config()
        except ConfigError:
            return  # shown on the Settings page; nothing starts until it is fixed
        if self.due(conn, config):
            self.start()


def _with_model(config: Config, model: str | None) -> Config:
    """The settings with another evaluator model, for comparing models on the answer key."""
    if model is None:
        return config
    evaluator = config.ai.evaluator.model_copy(update={"model": model})
    return config.model_copy(update={"ai": config.ai.model_copy(update={"evaluator": evaluator})})
