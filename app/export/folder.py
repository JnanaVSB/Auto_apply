"""Where an export may go, what its folders are called and how its files reach them. It knows nothing about jobs. The
files hold personal data outside the encrypted database, so whatever is made here is yours alone from its first moment,
and nothing you made or changed in the folder is replaced."""

import hashlib
import os
import re
import shutil
import unicodedata
from collections.abc import Callable, Iterable
from contextlib import suppress
from itertools import count
from pathlib import Path
from typing import BinaryIO

from app.config import PROJECT_DIR

CHOOSE = "Choose a folder first"
FULL_PATH = "Type the folder's full path, starting with / or ~ (or a drive letter on Windows)"
CONTROL_CHARACTER = "A folder's path can't hold a line break or another control character"
UNCHECKABLE = "That folder can't be checked: the path is too long, goes round in a loop, or isn't open to you"
IN_PROJECT = "That is the app's project folder, or inside it, which is public: choose a folder outside it"
IN_DATA = "That is the data folder, or inside it: choose a folder of its own"
TOO_WIDE = "That is your home folder, or a folder above it, the project or the data folder: choose a folder of its own"
IN_GIT = "That folder is inside a git project, which could publish these files: choose a folder outside it"
A_FILE = "That path leads to a file, not a folder"
DEEPER = "Only the last part of the path can be new: check it for typing mistakes, or make the folders above it first"
GONE = "The export folder isn't there: plug the drive in or choose it again"
NOT_ITS_PLACE = "A link or a file stands where this application's folder should be: move it away and export again"
BAD_SAVED_FOLDER = "This application's saved folder isn't a folder inside the export folder"
COMPANY_BYTES, NAME_BYTES = 55, 110  # so _2 and on stay within 120, on every computer
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"{port}{number}" for port in ("COM", "LPT") for number in "123456789¹²³")}
HISTORY = "status_history.txt"
OWN_PARTIALS = ("*.pdf.partial", "*.txt.partial")  # any other .partial in the folder is yours
NEW_FILE = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)  # never an existing file or link


class Refused(Exception):  # noqa: N818 (shown as it is)
    """One plain sentence naming the rule; never the path."""


def chosen(text: str, data_dir: Path) -> Path:
    """The typed folder as it really is, links followed, if the rules allow it; it may not exist yet."""
    if not text.strip():
        raise Refused(CHOOSE)
    if any(unicodedata.category(character) == "Cc" for character in text):
        raise Refused(CONTROL_CHARACTER)
    try:
        typed = Path(text).expanduser()
        if not typed.is_absolute():
            raise Refused(FULL_PATH)
        path = typed.resolve()
        _check(path, data_dir.resolve(), Path.home().resolve())
        if typed.is_symlink() and not typed.exists():
            raise Refused(GONE)
    except (OSError, ValueError, RuntimeError):
        raise Refused(UNCHECKABLE) from None
    return path


def make(text: str, data_dir: Path) -> Path:
    """Saving a folder: `chosen`, and a new last part is made, for you only."""
    path = chosen(text, data_dir)
    try:
        path.mkdir(mode=0o700, exist_ok=True)
    except FileNotFoundError:
        raise Refused(DEEPER) from None
    except OSError as error:
        raise Refused(f"That folder can't be made ({error.strerror})") from None
    return path


def ready(text: str, data_dir: Path) -> Path:
    """The start of a run: `chosen`, and it must be there. An unplugged drive is never made again as an empty folder."""
    path = chosen(text, data_dir)
    if not path.is_dir():
        raise Refused(GONE)
    return path


def name(company: str, role: str) -> str:
    """An application's folder name, safe on Linux, macOS and Windows: nothing from a job's text can make a path."""
    company, role = _cut(_clean(company), COMPANY_BYTES), _clean(role)
    found = _cut(f"{company}_{role}" if company and role else company or role or "application", NAME_BYTES)
    return f"{found}_" if found.upper() in RESERVED else found


def free(root: Path, day: str, base: str, taken: Iterable[str]) -> str:
    """The first of <day>/<base>, then _2, _3, with nothing at that path, not even a broken link, that isn't another
    application's already, in any case: its folder may be gone, or the disk may not tell Acme from ACME."""
    kept = {folder.casefold() for folder in taken}
    for number in count(1):
        relative = f"{day}/{base}" if number == 1 else f"{day}/{base}_{number}"
        if relative.casefold() not in kept and missing(root / relative):
            return relative


def enter(root: Path, relative: str) -> Path:
    """The application's folder inside `root`, made if needed, for you only, with the last visit's own .partial files
    cleared. Each step must really be that folder: nothing is made or cleared through a link."""
    parts = Path(relative).parts
    if not parts or Path(relative).anchor or ".." in parts:
        raise Refused(BAD_SAVED_FOLDER)
    root = root.resolve()
    folder = root
    for part in parts:
        folder = folder / part
        with suppress(FileExistsError):
            folder.mkdir(mode=0o700)
        if os.path.realpath(folder) != str(folder) or not folder.is_dir():
            raise Refused(NOT_ITS_PLACE)
    for pattern in OWN_PARTIALS:
        for left in folder.glob(pattern):
            if left.is_symlink() or left.is_file():
                left.unlink()
    return folder


def missing(path: Path) -> bool:
    """Nothing at all there. A link, a folder or a file with that name counts as there, and is left alone."""
    return not os.path.lexists(path)


def write(target: Path, data: bytes) -> None:
    _swapped_in(target, lambda file: file.write(data))


def copy(source: Path, target: Path) -> None:
    with source.open("rb") as found:
        _swapped_in(target, lambda file: shutil.copyfileobj(found, file))


def digest(path: Path) -> str | None:
    """A regular file's SHA-256, read a piece at a time; None for a link, a folder, nothing, or a file closed to you."""
    if not _regular(path):
        return None
    try:
        with path.open("rb") as file:
            return hashlib.file_digest(file, "sha256").hexdigest()
    except OSError:
        return None


def history(folder: Path, text: str, today: str) -> str:
    """The status history, the one file that changes after it is made: only by gaining lines. Anything else was changed
    since, so it is left, and the new text goes beside it, once. Says what happened: made, same, refreshed or beside."""
    target, new = folder / HISTORY, text.encode()
    if missing(target):
        write(target, new)
        return "made"
    if _regular(target):
        old = target.read_bytes()
        if old == new:
            return "same"
        if old.endswith(b"\n") and new.startswith(old):
            write(target, new)
            return "refreshed"
    if any(_regular(found) and found.read_bytes() == new for found in folder.glob("status_history-*.txt")):
        return "same"
    for number in count(1):
        beside = folder / (f"status_history-{today}.txt" if number == 1 else f"status_history-{today}-{number}.txt")
        if missing(beside):
            write(beside, new)
            return "beside"


def _check(path: Path, data: Path, home: Path) -> None:
    if _within(path, PROJECT_DIR):
        raise Refused(IN_PROJECT)
    if _within(path, data):
        raise Refused(IN_DATA)
    if any(_within(folder, path) for folder in (home, PROJECT_DIR, data)):
        raise Refused(TOO_WIDE)
    above = (path, *path.parents)
    if any(_there(found / ".git") for found in above):
        raise Refused(IN_GIT)
    nearest = next(found for found in above if found.exists())
    if not nearest.is_dir():
        raise Refused(A_FILE)
    if not os.access(nearest, os.R_OK | os.X_OK):
        raise Refused(UNCHECKABLE)


def _within(path: Path, folder: Path) -> bool:
    """`path` is `folder` or inside it, by name or as the same folder on disk (a disk that ignores case, a mount)."""
    return any(found == folder or _same(found, folder) for found in (path, *path.parents))


def _same(one: Path, other: Path) -> bool:
    try:
        return os.path.samefile(one, other)
    except (FileNotFoundError, NotADirectoryError):  # not made yet
        return False


def _there(path: Path) -> bool:
    """Like `missing`, but a folder closed to you raises: a .git that can't be looked for isn't known to be absent."""
    try:
        path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    return True


def _clean(text: str) -> str:
    """Letters, digits and the marks that belong to them, in any language; anything else is -."""
    kept = ""
    for character in unicodedata.normalize("NFC", text):
        kind = unicodedata.category(character)[0]
        kept += character if kind in "LN" or (kind == "M" and kept[-1:] not in ("", "-")) else "-"
    return re.sub("-+", "-", kept).strip("-")


def _cut(text: str, limit: int) -> str:
    """At most `limit` bytes, never half a character, with no separator left at the end."""
    return text.encode()[:limit].decode(errors="ignore").rstrip("-_")


def _regular(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _swapped_in(target: Path, fill: Callable[[BinaryIO], object]) -> None:
    """All or nothing: written beside its place, on the disk, then swapped in. Not pdf._write: data/documents is the
    app's own, but this folder is yours and may hold anything, so a .partial already there is never opened."""
    partial = target.with_name(f"{target.name}.partial")
    descriptor = os.open(partial, NEW_FILE, 0o600)
    try:
        with open(descriptor, "wb") as file:
            fill(file)
            file.flush()
            os.fsync(file.fileno())
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)
