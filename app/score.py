"""Score jobs: `python -m app.score` runs the quick check and the AI score; `python -m app.score setup` only
downloads the scoring files and checks Ollama."""

import argparse
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import NamedTuple

import sqlcipher3

from app import collect, db, llm
from app.config import Config, ConfigError, load_config
from app.fetch import Fetcher
from app.keychain import KeychainError
from app.llm import ollama
from app.resume import Resume, ResumeError, read_resume
from app.scoring import files, queue, stage_a
from app.scoring.meaning import Meaning

SCORE_LOCK = "score-lock.db"  # one scoring run at a time, from the terminal or the dashboard
BUSY = "Scoring is already running (in another window or the dashboard). Wait for it to finish."
WAITING = "Waiting for the collection to finish..."
STOPPED = "\nStopped. Everything scored so far is saved; run it again to continue."


def open_database(data_dir: Path, out: queue.Output = print) -> sqlcipher3.Connection:
    """Open the database (upgrading it if needed) the way the dashboard does: while briefly holding the collection
    lock, so an upgrade never runs during a collection. A phone-book refresh holding that lock has the database open
    already, so scoring goes on beside it; a collection is waited for."""
    waited = False
    while True:
        try:
            held = collect.lock(data_dir)
        except collect.CollectError:
            if not data_dir.is_dir() or collect.refresh_running(data_dir):
                return db.connect(data_dir)  # no data folder: connect names the fix; a refresh: already upgraded
            if not waited:
                out(WAITING)
                waited = True
            time.sleep(queue.PAUSE_CHECK_SECONDS)
            continue
        with closing(held):
            return db.connect(data_dir)


def setup(config: Config) -> bool:
    """Download what is missing and check Ollama; True when everything is ready."""
    with closing(Fetcher()) as fetcher:
        files.ensure_files(fetcher)
    print("Scoring files: all present and correct.")
    ready, line = ollama.status(config.ai.evaluator)
    print(line)
    return ready


class Prepared(NamedTuple):
    checker: stage_a.Checker
    setup: str
    resume: Resume


def prepare(config: Config, fetcher: Fetcher, out: queue.Output = print) -> Prepared:
    """What every scoring run needs, from the terminal or the dashboard: the files, the resume, the quick check."""
    folder = files.ensure_files(fetcher, out)
    resume = read_resume(config.data_dir)
    keywords, extra = config.roles.keywords, config.scoring.extra_skills
    checker = stage_a.Checker(keywords, stage_a.skill_names(folder, extra), resume.text, Meaning.load(folder))
    return Prepared(checker, stage_a.setup_fingerprint(resume, keywords, extra), resume)


def score(config: Config) -> None:
    with closing(Fetcher()) as fetcher:
        prepared = prepare(config, fetcher)
    with closing(open_database(config.data_dir)) as conn:
        queue.run(conn, config, *prepared)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.score", description="Score the collected jobs.")
    parser.add_argument("command", nargs="?", choices=["setup"], help="only download the scoring files, check Ollama")
    args = parser.parse_args(argv)
    try:
        config = load_config()
        with closing(collect.lock(config.data_dir, SCORE_LOCK, BUSY)):  # also stops two downloads of the same file
            if args.command == "setup":
                return 0 if setup(config) else 1
            score(config)
    except (
        ConfigError, KeychainError, db.DatabaseError, collect.CollectError, files.SetupError, ResumeError,
        llm.StopScoring, sqlcipher3.Error, sqlite3.Error, OSError,
    ) as error:  # fmt: skip
        print(db.error_line(error), file=sys.stderr)
    except (EOFError, KeyboardInterrupt):
        print(STOPPED, file=sys.stderr)
    else:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
