"""Run the council from the terminal, like leaving the dashboard open: python -m app.prepare

A pass every minute (app/council/runner.py), each first starting what automatic preparation may start, until Ctrl+C.
One runner at a time: while a dashboard runs the council, this waits its turn. Everything is saved step by step, so
Ctrl+C, even in the middle of a call, loses nothing: the next start counts that call honestly and carries on. Each pass
prints one line of counts, never your text."""

import sys
import time
from contextlib import closing
from datetime import datetime

import sqlcipher3

from app import collect, db
from app.config import ConfigError, load_config
from app.council import automatic, runner

PAUSE_SECONDS = 60
BUSY = "The council is running in a dashboard: this waits its turn."
COUNTS = """SELECT (SELECT count(*) FROM preparations WHERE status = 'writing'),
    (SELECT count(*) FROM steps WHERE status = 'sent' AND mode = 'batch'),
    (SELECT count(*) FROM steps WHERE status = 'waiting')"""


def main(argv: list[str] | None = None) -> int:
    try:
        config = load_config()
    except ConfigError as error:
        print(error, file=sys.stderr)
        return 1
    state = runner.State()
    try:
        while True:
            print(one_pass(config, state), flush=True)
            time.sleep(PAUSE_SECONDS)
    except KeyboardInterrupt:
        print("Stopped. Everything finished so far is saved; run the command again to carry on.")
        return 0


def one_pass(config, state: runner.State) -> str:
    """One pass under the runner's lock; returns its line."""
    now = f"{datetime.now():%H:%M}"
    try:
        with closing(collect.lock(config.data_dir, runner.LOCK, BUSY)), closing(db.connect(config.data_dir)) as conn:
            config = load_config()
            waiting = automatic.start_due(conn, config)
            saved = runner.run(conn, config, state)
            working, at_anthropic, waits = conn.execute(COUNTS).fetchone()
    except collect.CollectError as error:
        return f"{now} {error}"
    except ConfigError as error:
        return f"{now} The settings can't be read: {error}"
    except (db.DatabaseError, sqlcipher3.Error, OSError) as error:
        return f"{now} {db.error_line(error)}"
    parts = [f"{saved} answers saved", f"{working} versions working", f"{at_anthropic} calls at Anthropic",
             f"{waits} calls waiting"]  # fmt: skip
    parts += [f"paused: {why}" for why in state.paused.values()] + ([waiting] if waiting else [])
    return f"{now} " + "; ".join(parts)


if __name__ == "__main__":
    sys.exit(main())
