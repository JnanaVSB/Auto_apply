"""The rest of the one-command setup, after `setup.sh` made the environment: `python -m app.setup`.

Each step is skipped when it is already done, so running it again is safe: nothing is overwritten or asked twice.
"""

import argparse
import secrets
import shlex
import sys
from contextlib import closing
from urllib.parse import urlsplit

from app import collect, config, db, keychain, score
from app.config import Config, ConfigError, load_config
from app.keychain import KeychainError
from app.scoring import files
from app.web import __main__ as web_command

CHECK_NAME = "setup-check"  # the keychain test entry, always removed again
STOPPED = "\nStopped. Nothing done so far is undone; run ./setup.sh again to continue."
SETTINGS_LEFT = "Settings: data/config.yaml is already there; left as it is."
DATA_IS_A_FILE = "The project's data folder is a file: rename or move the file named data, then run ./setup.sh again."
NO_KEYCHAIN_LINUX = (
    "This computer has no keychain the app can use. On a Linux desktop, install GNOME Keyring (or KWallet) and log in "
    "to the desktop; a server with no desktop has none, so use a computer with a desktop."
)
REFUSED_LINUX = (
    'The keychain is locked or refused: unlock it (log out and back in, or open "Passwords and Keys"), '
    "then run ./setup.sh again."
)
NO_KEYCHAIN_MAC = (
    "The keychain library is set to a store that isn't the macOS Keychain (for example by PYTHON_KEYRING_BACKEND or "
    "a keyringrc.cfg file): remove that setting, then run ./setup.sh again."
)
REFUSED_MAC = (
    "macOS asked for permission and it was refused, or the keychain is locked: run ./setup.sh again and click "
    '"Always Allow" when macOS asks.'
)
OLLAMA_NOTE = (
    'AI scoring runs through Ollama on this computer: install it and its model as the README\'s "Scoring jobs" '
    "section says, or choose another on Settings → Scoring model."
)


class SetupError(Exception):
    """A step that cannot go on; the message says why and what to do."""


def _ask(question: str) -> bool:
    return input(f"{question} [y/N] ").strip().lower() in {"y", "yes"}


def _keychain_fix(missing: bool) -> str:
    if sys.platform == "darwin":
        return NO_KEYCHAIN_MAC if missing else REFUSED_MAC
    if sys.platform.startswith("linux"):
        return NO_KEYCHAIN_LINUX if missing else REFUSED_LINUX
    return ""


def _check_keychain() -> None:
    """Save, read and remove a random test value, before any password is asked for."""
    missing = True  # until the keychain itself is found
    try:
        keychain.safe_backend()  # the same check every read and write makes: refused here means there is none
        missing = False
        value = secrets.token_hex(16)
        keychain.set_secret(CHECK_NAME, value)
        try:
            same = keychain.get_secret(CHECK_NAME) == value
        finally:
            keychain.delete_secret(CHECK_NAME)
        if not same:
            raise KeychainError("The OS keychain gave back a different value than the one saved.")
    except KeychainError as error:
        raise SetupError(f"{error}\n{_keychain_fix(missing)}".rstrip()) from None
    print("Keychain: works.")


def _copy_config() -> None:
    """data/config.yaml from config.example.yaml, only if there is none: an existing one is never touched."""
    path = config.CONFIG_PATH  # looked up when called, as load_config does
    if path.exists():
        print(SETTINGS_LEFT)
        return
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except FileExistsError:
        raise SetupError(DATA_IS_A_FILE) from None
    example = (config.PROJECT_DIR / "config.example.yaml").read_text(encoding="utf-8")
    try:
        file = path.open("x", encoding="utf-8")  # "x": never overwrites, even a file made meanwhile
    except FileExistsError:
        print(SETTINGS_LEFT)
        return
    try:
        with file:
            file.write(example)
    except BaseException:  # a half-written file would be left alone by the next run, so it goes
        path.unlink(missing_ok=True)
        raise
    print("Settings: data/config.yaml made from config.example.yaml.")


def _later(command: str) -> str:
    """The exact line that runs an app command later: real paths, shell-quoted, with this environment's Python, so it
    works with no environment activated."""
    return f"cd {shlex.quote(str(config.PROJECT_DIR))} && {shlex.quote(sys.executable)} -m {command}"


def _scoring(settings: Config) -> None:
    folder = files.cache_dir()
    try:
        missing = [file for file in files.FILES if not files.is_correct(folder / file.name, file)]
    except OSError:  # an unreadable cache folder: the download then says what is wrong
        missing = list(files.FILES)
    size = sum(file.size for file in missing) / 1_000_000
    if not missing or _ask(f"Download the scoring files now (about {size:.0f} MB)?"):
        try:  # as `python -m app.score setup` does, but Ctrl+C stops setup itself
            with closing(collect.lock(settings.data_dir, score.SCORE_LOCK, score.BUSY)):
                ready = score.setup(settings)
        except score.ERRORS as error:
            print(db.error_line(error), file=sys.stderr)
            ready = False
        if not ready:  # doesn't stop setup
            print(
                "Scoring isn't ready yet (see the line above); once that's fixed, check again with:\n"
                f"  {_later('app.score setup')}"
            )
    else:
        print(f"Scoring files not downloaded; get them later with:\n  {_later('app.score setup')}")
    evaluator = settings.ai.evaluator
    if evaluator.service == "ollama":
        print(OLLAMA_NOTE)
    else:
        print(
            f"The scoring model is {urlsplit(evaluator.address).hostname}; change it on Settings → Scoring model "
            "(see docs/own-ai-address.md)."
        )


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(
        prog="python -m app.setup", description="Finish setting up after ./setup.sh; safe to run again."
    ).parse_args(argv)
    try:
        _check_keychain()
        _copy_config()
        settings = load_config()
        if not (settings.data_dir / db.DB_FILE).exists():
            if db.main(["setup"]) != 0:  # it prints its own reason
                return 1
        elif keychain.get_secret(db.KEY_NAME) is None:  # e.g. the data folder copied to a new computer
            print(
                "Database: found, but its key isn't in this computer's keychain (a new computer?). Restore it with:\n"
                f"  {_later('app.db recover')}"
            )
        else:
            print("Database: already set up.")
        _scoring(settings)
        print(
            f"Setup is done. To open the dashboard later, run:\n  {_later('app.web')}\n"
            "Roles and every other setting are chosen on its Settings page; nobody needs to edit the settings file."
        )
        if _ask("Start the dashboard now?"):
            return web_command.main([])
    except (SetupError, ConfigError, KeychainError, OSError) as error:
        print(db.error_line(error), file=sys.stderr)
    except (EOFError, KeyboardInterrupt):
        print(STOPPED, file=sys.stderr)
    else:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
