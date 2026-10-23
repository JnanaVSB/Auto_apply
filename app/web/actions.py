"""Dashboard actions that change data: not interested, fix location, add a job, add a company, save the settings.

Nothing here ever deletes anything. The two "add" actions contact job sites, so they run beside the dashboard with
their own database connection.
"""

import re
import unicodedata
from contextlib import closing
from pathlib import Path

import sqlcipher3
import yaml

from app import collect, db, jobs
from app import config as settings_file
from app.config import Config, ConfigError, load_config
from app.fetch import Fetcher, FetchError, NotFound
from app.keychain import KeychainError
from app.sources import BOARD_NAME, SourceError
from app.web import queries

KINDS = ("greenhouse", "lever", "ashby")
LEGAL_ENDINGS = {"inc", "llc", "ltd", "corp", "corporation", "co", "company"}
MAX_NAME = 100
MAX_LISTS = 20  # Simplify list rows on the settings form
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
    answers = {"Company: ": company, "Job title: ": title}  # what `collect add` asks for in the terminal
    try:
        with closing(db.connect(data_dir)) as conn:
            message = collect.add(conn, fetcher, link, answers.__getitem__)
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


def add_company(data_dir: Path, fetcher: Fetcher, text: str) -> list[str]:
    """Find a company's board on Greenhouse, Lever and Ashby; save its matching jobs and read it on every collection.

    `text` is a company name, or one exact board such as greenhouse:acme. Returns what happened, one line each.
    """
    text = text.strip()[:MAX_NAME]
    kind, _, name = text.lower().partition(":")
    tries = [(kind, name)] if kind in KINDS and BOARD_NAME.fullmatch(name) else [
        (kind, name) for kind in KINDS for name in board_names(text)
    ]  # fmt: skip
    if not tries:
        return ["Type a company name, for example Acme AI, or one board such as greenhouse:acme."]
    try:
        config = load_config()
        with closing(db.connect(data_dir)) as conn:
            return _find_boards(conn, fetcher, config, tries, text)
    except (ConfigError, KeychainError, db.DatabaseError) as error:
        return [str(error)]


def _find_boards(
    conn: sqlcipher3.Connection, fetcher: Fetcher, config: Config, tries: list[tuple[str, str]], text: str
) -> list[str]:
    roles = jobs.Roles.from_settings(config.roles.simplify_categories, config.roles.keywords)
    lines, done, answered = [], set(), False  # done: sites with a board found, or that could not be read
    for kind, name in tries:
        if kind in done:
            continue
        board = f"{kind}:{name}"
        try:
            data, _ = fetcher.get_json(collect.BOARDS[kind].api_url(name))
            found, _ = collect.BOARDS[kind].parse(data, name)
        except NotFound:
            continue
        except (FetchError, SourceError) as error:
            lines.append(f"{kind.title()} could not be read ({error}).")
            done.add(kind)
            continue
        done.add(kind)
        answered = True
        if not found:  # a guessed name with no jobs may be another company's empty board
            lines.append(f"{board} exists but lists no jobs right now, so it was not added.")
        elif conn.execute("SELECT 1 FROM sources WHERE name = ? AND retired_on IS NULL", (board,)).fetchone():
            lines.append(f"{board} is already read on every collection.")
        else:
            counts = jobs.save(conn, board, found, roles, collect._now())
            with conn:  # remembered (and no longer retired), so every collection reads it from now on
                conn.execute(collect.SAVE_SOURCE, (board, None, collect._now()))
            lines.append(
                f"Found and added: {board} ({len(found):,} jobs on the board, {counts.kept:,} match your roles "
                "and are saved)."
            )
    if not answered:
        tried = ", ".join(dict.fromkeys(name for _, name in tries))
        lines.append(f"No Greenhouse, Lever or Ashby board found for '{text}' (tried {tried}). {NOT_FOUND_TIP}")
    return lines


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
        "collect_every_hours": str(raw.get("collect_every_hours", 0)),
    }
    for number, listing in enumerate(sources.get("simplify_lists") or []):
        form[f"list_url_{number}"], form[f"list_level_{number}"] = listing["url"], listing["level"]
    return form


def save_settings(form: dict[str, str]) -> str | None:
    """Check the form's settings with the usual rules, then swap them in all at once; returns the problem, or None.

    The data folder is kept exactly as written in the file; it is changed by hand only.
    """
    path, hours = settings_file.CONFIG_PATH, form.get("collect_every_hours", "").strip()
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
                    for number in range(MAX_LISTS + 1)
                    if form.get(f"list_url_{number}", "").strip()
                ],
                "extra_boards": _lines(form.get("extra_boards", "")),
            },
            "collect_every_hours": int(hours) if hours.isdigit() else hours,
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
