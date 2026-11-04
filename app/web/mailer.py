"""The Gmail sync beside the dashboard (app/gmail/sync.py): a pass when it is due (Settings: every 30 minutes, or off),
and at once after "Sync now", under its own lock, so a second dashboard never reads the same mail at the same time.
Everything is saved e-mail by e-mail, so closing the dashboard loses nothing: the next pass carries on."""

import logging
import threading
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import sqlcipher3

from app import collect, db
from app.config import ConfigError, load_config
from app.council import runner
from app.gmail import client, sync
from app.keychain import KeychainError, get_secret

LOCK = "gmail-lock.db"
BUSY = "A Gmail sync is running in another dashboard; it carries on there."
PERIOD_BUSY = "A Gmail sync is running in another dashboard; press Read this period again in a moment."
NOT_CONNECTED = "Gmail isn't connected (Settings → Gmail)."
log = logging.getLogger("app.web")


@dataclass
class Mailer:
    data_dir: Path
    transport: httpx.BaseTransport | None = None  # tests set a fake Google
    notice: str | None = None  # the last pass's words, for Settings → Gmail
    period_notice: str | None = None  # the last period's words ("Read this period"), for Settings → Gmail
    tried: datetime | None = None  # the last pass's start: a failing pass waits the set minutes too
    period: tuple[date, date] | None = None  # a period to read, waiting for its turn
    _again: bool = False
    _thread: threading.Thread | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def wake(self) -> None:
        """A press of Sync now, or a sync that is due: a pass now (or right after the one running)."""
        with self._lock:
            self._again = True
            self._start()

    def read_period(self, start: date, end: date) -> None:
        """Read this period's e-mails once, in the background (right after the pass running, if one is); a second
        press before it starts replaces the first."""
        with self._lock:
            self.period = start, end
            self._start()

    def _start(self) -> None:
        """With the lock held: the passes' thread, unless it is running."""
        if self._thread is None:
            self._thread = threading.Thread(target=self._passes, daemon=True)
            self._thread.start()

    def tick(self, conn: sqlcipher3.Connection) -> None:
        """Part of the once-a-minute check: a pass when Gmail is connected and the set minutes have gone by."""
        try:
            minutes = load_config().gmail_every_minutes
            connected = minutes and get_secret(client.REFRESH)
        except (ConfigError, KeychainError):  # shown on the Settings pages
            return
        if not connected:
            return
        row = conn.execute("SELECT last_sync_on FROM gmail_sync WHERE id = 1").fetchone()
        last = max(filter(None, (datetime.fromisoformat(row[0]) if row and row[0] else None, self.tried)), default=None)
        if last is None or datetime.now(UTC) - last >= timedelta(minutes=minutes):
            self.wake()

    def _passes(self) -> None:
        """A waiting period first, then a waiting pass, until neither waits."""
        while True:
            with self._lock:
                period, again = self.period, self._again
                self.period, self._again = None, False
                if not (period or again):
                    self._thread = None
                    return
            if period:
                self._pass(period)
            if again:
                self._pass()

    def _pass(self, period: tuple[date, date] | None = None) -> None:
        """A regular pass, or a chosen period's; their words go to `notice` and `period_notice`."""
        if period is None:
            self.tried = datetime.now(UTC)
            self.notice = self._words(None)
        else:
            self.period_notice = self._words(period)

    def _words(self, period: tuple[date, date] | None) -> str:
        try:
            with closing(collect.lock(self.data_dir, LOCK, BUSY)), closing(db.connect(self.data_dir)) as conn:
                config = load_config()
                sign_in = get_secret(client.CLIENT_ID), get_secret(client.CLIENT_SECRET), get_secret(client.REFRESH)
                if not all(sign_in):
                    return NOT_CONNECTED
                with client.Gmail(*sign_in, transport=self.transport) as gmail:
                    # the local model waits while scoring uses the graphics card
                    role, scoring = config.ai.evaluator, lambda: runner._scoring(config)
                    if period:
                        return sync.period(conn, gmail, *period, role, scoring, config.gmail_read_all)
                    return sync.run(conn, gmail, config.gmail_lookback_days, datetime.now(UTC), role, scoring,
                                    config.gmail_read_all)  # fmt: skip
        except (client.GmailError, KeychainError, collect.CollectError) as error:
            return PERIOD_BUSY if period and str(error) == BUSY else str(error)
        except ConfigError as error:
            return f"The settings can't be read: {error}"
        except (db.DatabaseError, sqlcipher3.Error, OSError) as error:
            return db.error_line(error)
        except Exception as error:  # the type only: its text could hold an e-mail's words
            log.error("Unexpected error in the Gmail sync: %s", type(error).__name__)
            return "The Gmail sync stopped on an unexpected error; the next sync tries again."
