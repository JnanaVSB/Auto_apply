"""The council's runner beside the dashboard (app/council/runner.py): a pass every minute, and at once after a press,
under its own lock, so the terminal command or a second dashboard never sends at the same time. Each pass first starts
what automatic preparation may start (app/council/automatic.py). Everything is saved step by step in the database, so
closing the dashboard loses nothing: the next pass, here or in the terminal, carries on."""

import logging
import threading
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import sqlcipher3

from app import collect, db
from app.config import ConfigError, load_config
from app.council import automatic, runner

BUSY = "The council is running in another dashboard or the terminal: it carries on there."
log = logging.getLogger("app.web")


@dataclass
class Preparer:
    data_dir: Path
    notice: str | None = None  # why the last pass could not run; shown in the job's box
    waiting: str | None = None  # why automatic preparation waits
    state: runner.State = field(default_factory=runner.State)  # services paused, until you save keys or settings
    _again: bool = False
    _thread: threading.Thread | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def wake(self) -> None:
        """A press, or the minute's check: a pass now (or right after the one running)."""
        with self._lock:
            self._again = True
            if self._thread is None:
                self._thread = threading.Thread(target=self._passes, daemon=True)
                self._thread.start()

    def tick(self, conn: sqlcipher3.Connection) -> None:
        """Part of the once-a-minute check while the dashboard is open."""
        self.wake()

    def resume(self) -> None:
        """You saved a key or the settings: paused services are tried again."""
        self.state = runner.State()
        self.wake()

    def _passes(self) -> None:
        while True:
            with self._lock:
                if not self._again:
                    self._thread = None
                    return
                self._again = False
            self._pass()

    def _pass(self) -> None:
        try:
            with closing(collect.lock(self.data_dir, runner.LOCK, BUSY)), closing(db.connect(self.data_dir)) as conn:
                config = load_config()
                self.waiting = automatic.start_due(conn, config)
                runner.run(conn, config, self.state)
            self.notice = None
        except collect.CollectError as error:
            self.notice = str(error)
        except ConfigError as error:
            self.notice = f"The settings can't be read: {error}"
        except (db.DatabaseError, sqlcipher3.Error, OSError) as error:
            self.notice = db.error_line(error)
        except Exception as error:  # one line only; never job data, your text or a crash report
            log.error("Unexpected error in the council: %s", type(error).__name__)
            self.notice = f"The council stopped on an unexpected error ({type(error).__name__}); it tries again."
