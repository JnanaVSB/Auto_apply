"""Dashboard actions: not interested, fix location, add a job, find and add a company, stop reading a board,
save the settings.

Nothing here ever deletes anything. The two "add" actions contact job sites, so they run beside the dashboard with
their own database connection.
"""

import re
import threading
import unicodedata
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import sqlcipher3
import yaml

from app import collect, db, jobs
from app import config as settings_file
from app.config import Config, ConfigError, load_config
from app.fetch import Fetcher, FetchError, NotFound
from app.keychain import KeychainError
from app.sources import BOARD_NAME, SourceError
from app.sources.boards import BOARDS
from app.web import queries

LEGAL_ENDINGS = {"inc", "llc", "ltd", "corp", "corporation", "co", "company"}
MAX_NAME = 100
ADDING = threading.Lock()  # the two "add" actions run one at a time, so a double click finds "Already saved"
LIST_ROW = re.compile(r"list_url_([0-9]{1,4})")  # one Simplify list on the settings form; there is no limit
NUMBERS = ("collect_every_hours", "phonebook_every_days", "requests_per_second")  # whole numbers on the settings form
SETTINGS_HEADER = "# auto_apply settings, saved by the dashboard. Each setting is explained in config.example.yaml.\n"
NOT_FOUND_TIP = (
    "Tip: open the company's careers page; if its address contains greenhouse.io, lever.co or ashbyhq.com, type the "
    "name from that address (for example greenhouse:acme), or paste one job's link into Add job."
)


def set_not_interested(conn: sqlcipher3.Connection, job_id: int, value: bool) -> bool:
    """Hide the job from the default list, or show it again; False when there is no such job."""
    with conn:
        rows = conn.execute("UPDATE jobs SET not_interested = ? WHERE id = ?", (value, job_id))
    return rows.rowcount == 1


def set_location(conn: sqlcipher3.Connection, job_id: int, choice: str) -> bool:
    """'yes' or 'no' set by hand, which collections never change; 'auto' lets collections decide again."""
    with conn:
        if choice == "auto":
            rows = conn.execute("UPDATE jobs SET location_by_you = 0 WHERE id = ?", (job_id,))
        else:
            sql = "UPDATE jobs SET us_location = ?, location_by_you = 1, check_location = 0 WHERE id = ?"
            rows = conn.execute(sql, (choice, job_id))
    return rows.rowcount == 1


def add_job(data_dir: Path, fetcher: Fetcher, link: str, company: str, title: str) -> tuple[str, int | None]:
    """`collect add` with its own rules; the message, and the job's number when it is saved."""
    try:
        with ADDING, closing(db.connect(data_dir)) as conn:
            message = collect.add(conn, fetcher, link, details=(company, title))
            return message, queries.job_id_by_link(conn, link)
    except (ConfigError, KeychainError, db.DatabaseError, collect.CollectError) as error:
        return str(error), None
    except (FetchError, SourceError) as error:
        return f"The job's board could not be read ({error}).", None


def board_names(company: str) -> list[str]:
    """Likely board names: letters and digits joined, words joined by dashes, and without a legal ending (Inc...)."""
    plain = unicodedata.normalize("NFKD", company).encode("ascii", "ignore").decode().lower()  # Société -> societe
    words = re.findall(r"[a-z0-9]+", plain)
    shorter = list(words)
    while len(shorter) > 1 and shorter[-1] in LEGAL_ENDINGS:
        shorter.pop()
    names = ("".join(words), "-".join(words), "".join(shorter))
    return [name for name in dict.fromkeys(names) if BOARD_NAME.fullmatch(name)]


@dataclass
class FoundBoard:
    """A board that may belong to the company the user typed; shown for confirmation, not saved yet."""

    board: str  # e.g. greenhouse:acme
    company: str | None  # the name the board reports; Lever and Ashby report none
    listed: int  # jobs on the board
    matching: int  # of those, jobs a collection would save (your roles, not clearly outside the US)
    titles: list[str]  # its first few job titles
    page: str  # the board's own public page, to check by hand
    known: bool  # already read on every collection
    stopped: bool  # stopped by you


def find_company(data_dir: Path, fetcher: Fetcher, text: str) -> tuple[list[FoundBoard], list[str]]:
    """Look for a company's boards on Greenhouse, Lever and Ashby. Saves nothing; returns every board found and notes.

    `text` is a company name, or one exact board such as greenhouse:acme.
    """
    text = text.strip()[:MAX_NAME]
    kind, _, name = text.lower().partition(":")
    tries = [(kind, name)] if kind in BOARDS and BOARD_NAME.fullmatch(name) else [
        (kind, name) for kind in BOARDS for name in board_names(text)
    ]  # fmt: skip
    if not tries:
        return [], ["Type a company name, for example Acme AI, or one board such as greenhouse:acme."]
    try:
        config = load_config()
        with closing(db.connect(data_dir)) as conn:
            return _find_boards(conn, fetcher, config, tries, text)
    except (ConfigError, KeychainError, db.DatabaseError) as error:
        return [], [str(error)]


def _find_boards(
    conn: sqlcipher3.Connection, fetcher: Fetcher, config: Config, tries: list[tuple[str, str]], text: str
) -> tuple[list[FoundBoard], list[str]]:
    """Every name tried on its site, all sites at the same time; every board that exists is offered."""
    roles = jobs.Roles.from_settings(config.roles)
    names = [f"{kind}:{name}" for kind, name in tries]
    with closing(collect.download_boards(fetcher, names, set())) as downloads:  # a few names per site: none skipped
        replies = {board: download for _, board, download in downloads}
    boards, notes, failed = [], [], set()  # failed: sites that could not be read
    answered = False
    for board in names:  # in the order tried, so the page always lists them the same way
        kind, _, name = board.partition(":")
        download = replies[board]
        if isinstance(download, NotFound):
            continue
        if isinstance(download, FetchError | SourceError):
            if kind not in failed:  # one note per site
                notes.append(f"{kind.title()} could not be read ({download}).")
                failed.add(kind)
            continue
        if isinstance(download, BaseException):
            raise download  # a fault in the app
        found = download[0]
        answered = True
        if not found:  # a guessed name with no jobs may be another company's empty board
            notes.append(f"{board} exists but lists no jobs right now, so it cannot be added.")
            continue
        state = conn.execute("SELECT retired_on IS NULL, stopped_on IS NOT NULL FROM sources WHERE name = ?", (board,))
        read, stopped = state.fetchone() or (False, False)
        boards.append(
            FoundBoard(
                board,
                found[0].company if kind == "greenhouse" else None,
                len(found),
                sum(jobs.wanted(job, roles) for job in found),
                [job.title for job in found[:3]],
                BOARDS[kind].BOARD_PAGE.format(name),
                bool(read and not stopped),
                bool(stopped),
            )
        )
    if not answered:
        tried = ", ".join(dict.fromkeys(name for _, name in tries))
        notes.append(f"No Greenhouse, Lever or Ashby board found for '{text}' (tried {tried}). {NOT_FOUND_TIP}")
    return boards, notes


def add_board(data_dir: Path, fetcher: Fetcher, board: str) -> str:
    """After the user confirmed it: save one board's matching jobs and read it on every collection."""
    kind, _, name = board.partition(":")
    if kind not in BOARDS or not BOARD_NAME.fullmatch(name):
        return "That is not a board name such as greenhouse:acme."
    try:
        config = load_config()
        roles = jobs.Roles.from_settings(config.roles)
        with ADDING, closing(db.connect(data_dir)) as conn:
            found, _ = collect.read_board(fetcher, kind, name)
            if not found:
                return f"{board} lists no jobs right now, so it was not added."
            counts = jobs.save(conn, board, found, roles, collect.now())
            with conn:  # remembered (no longer retired or stopped), so every collection reads it from now on
                conn.execute(collect.SAVE_SOURCE, (board, None, collect.now()))
                conn.execute("UPDATE sources SET stopped_on = NULL WHERE name = ?", (board,))
                conn.execute("UPDATE phonebook SET result = 'added' WHERE board = ? AND result != 'added'", (board,))
    except NotFound:
        return f"{board} was not found; it may have closed."
    except (FetchError, SourceError) as error:
        return f"{kind.title()} could not be read ({error})."
    except (ConfigError, KeychainError, db.DatabaseError) as error:
        return str(error)
    return (
        f"Added: {board} ({len(found):,} jobs on the board, {counts.kept:,} match your roles and are saved). "
        "It is read on every collection from now on."
    )


def set_board_stopped(conn: sqlcipher3.Connection, board: str, stopped: bool) -> bool:
    """Stop reading a company board, or read it again. Deletes nothing; False when there is no such board."""
    with conn:
        rows = conn.execute(
            "UPDATE sources SET stopped_on = ? WHERE name = ? AND name NOT LIKE 'simplify:%'",
            (collect.now()[:10] if stopped else None, board),
        )
    return rows.rowcount == 1


def decline_board(conn: sqlcipher3.Connection, board: str) -> bool:
    """A board the cross-check found is not the company you want: it is never offered again. Deletes nothing."""
    with conn:
        rows = conn.execute(
            "UPDATE phonebook SET result = 'declined' WHERE board = ? AND result = 'to confirm'", (board,)
        )
    return rows.rowcount == 1


def _whole(text: str) -> int | str:
    """A whole number typed in the form; anything else is kept as typed, so the settings check names it."""
    text = text.strip()
    return int(text) if re.fullmatch(r"[0-9]{1,9}", text) else text  # "²" is not a number


def _lines(text: str) -> list[str]:
    """One entry per line: spaces trimmed, empty lines left out, repeats kept once."""
    return list(dict.fromkeys(line.strip() for line in text.splitlines() if line.strip()))


def settings_form() -> dict[str, str]:
    """The settings file as the settings form shows it, values as written in the file."""
    raw = yaml.safe_load(settings_file.CONFIG_PATH.read_text(encoding="utf-8"))
    roles, sources = raw.get("roles") or {}, raw.get("sources") or {}
    form = {
        "data_dir": str(raw["data_dir"]),
        "simplify_categories": "\n".join(roles.get("simplify_categories") or []),
        "keywords": "\n".join(roles.get("keywords") or []),
        "extra_boards": "\n".join(sources.get("extra_boards") or []),
        "phonebook_lists": "\n".join(sources.get("phonebook_lists") or []),
        "collect_every_hours": str(raw.get("collect_every_hours", 0)),
        "phonebook_every_days": str(
            raw.get("phonebook_every_days", Config.model_fields["phonebook_every_days"].default)
        ),
        "requests_per_second": str(raw.get("requests_per_second", 1)),
    }
    for number, listing in enumerate(sources.get("simplify_lists") or []):
        form[f"list_url_{number}"], form[f"list_level_{number}"] = listing["url"], listing["level"]
    return form


def save_settings(form: dict[str, str]) -> str | None:
    """Check the form's settings with the usual rules, then swap them in all at once; returns the problem, or None.

    The data folder is kept exactly as written in the file; it is changed by hand only.
    """
    path = settings_file.CONFIG_PATH
    rows = sorted(int(match[1]) for match in map(LIST_ROW.fullmatch, form) if match)  # every list on the form
    new = path.with_name(f"{path.name}.new")
    try:
        settings = {
            "data_dir": yaml.safe_load(path.read_text(encoding="utf-8"))["data_dir"],
            "roles": {
                "simplify_categories": _lines(form.get("simplify_categories", "")),
                "keywords": _lines(form.get("keywords", "")),
            },
            "sources": {
                "simplify_lists": [
                    {"url": form[f"list_url_{number}"].strip(), "level": form.get(f"list_level_{number}", "")}
                    for number in rows
                    if form[f"list_url_{number}"].strip()
                ],
                "extra_boards": _lines(form.get("extra_boards", "")),
                "phonebook_lists": _lines(form.get("phonebook_lists", "")),
            },
            **{name: _whole(form.get(name, "")) for name in NUMBERS},
        }
        new.write_text(SETTINGS_HEADER + yaml.safe_dump(settings, sort_keys=False, allow_unicode=True), "utf-8")
        new.chmod(0o600)
        load_config(new)  # the very rules used when data/config.yaml is read
        new.replace(path)  # all at once: never a half-written settings file
    except ConfigError as error:
        return str(error)
    except (yaml.YAMLError, KeyError, TypeError):
        return "data/config.yaml could not be read. Fix it by hand (config.example.yaml shows the layout)."
    except OSError as error:  # only the file name, since full paths include the user's name
        return f"The settings could not be saved ({error.strerror or error}: {Path(error.filename or path).name})."
    finally:
        new.unlink(missing_ok=True)
    return None
