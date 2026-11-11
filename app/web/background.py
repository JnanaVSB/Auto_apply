"""The person's own background picture: a private file in the data folder, shown behind every signed-in page and
served to the signed-in person only. It never enters the repository; a missing file means the built-in pictures."""

import io
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from starlette.exceptions import HTTPException

FOLDER = "background"  # inside the data folder
NAMES = ("own.jpg", "own.png", "own.webp")  # fixed names; the first found is used, the type comes from its bytes
CREDIT = "own.txt"  # the credit line, typed by the person; its first line is shown
ROUTE = "/background/own"  # named once in the stylesheet, so it never changes with the file
INK = "dark"  # white words: a dark picture; the light/dark switch comes with the Background choice on Settings
DEFAULT_CREDIT = "Private background picture, not part of this project."
MAX_CREDIT = 300
MAX_BYTES = 15 * 1024 * 1024  # a 4K photograph is a few MB; anything larger is the wrong file
CHUNK = 64 * 1024
# The file itself, never a link to somewhere else, and a pipe or device under the name must not hold the dashboard.
# Windows has neither flag, so a link the person made under the name is followed there (its target still has to be a
# plain picture file within the size cap); it has no named pipes in folders; O_BINARY keeps the bytes as they are.
# Each flag counts for 0 where the system lacks it.
OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
NOT_A_PICTURE = (
    "{} in your data folder's background folder is not a JPEG, PNG or WebP picture of up to 15 MB (a plain file, "
    "not a link), or can't be read; the built-in pictures are shown meanwhile."
)
_seen: dict[Path, tuple[tuple, "Own"]] = {}  # per folder: the files' sizes and times last looked at, and the answer


@dataclass(frozen=True)
class Own:
    """What the pages need: whether an own picture is in use, its words colour and credit, or why it is not."""

    in_use: bool = False
    ink: str = INK
    credit: str = DEFAULT_CREDIT
    problem: str | None = None


def add_routes(app: FastAPI, data_dir: Path) -> None:
    folder = data_dir / FOLDER
    app.state.background = folder  # read by main._page for every page

    @app.get(ROUTE)
    async def own_picture(request: Request) -> Response:
        """The private file, as the picture type its bytes say; 404 when none is set, so the floor colour shows.
        The file is opened once: what was checked is what is sent, whatever happens to the name meanwhile."""
        found = _first(folder)
        if found is None or found[1] is None:
            raise HTTPException(404)
        file, details = found[1]
        try:
            kind = _kind(file.read(12))
            tag = f'"{details.st_ino:x}-{details.st_mtime_ns:x}-{details.st_size:x}"'
            if kind and request.headers.get("if-none-match") != tag:
                file.seek(0)
                headers = {"ETag": tag, "Content-Length": str(details.st_size)}
                return StreamingResponse(_chunks(file, details.st_size), media_type=kind, headers=headers)
        except BaseException:
            file.close()
            raise
        file.close()
        if kind is None:
            raise HTTPException(404)
        return Response(status_code=304, headers={"ETag": tag})  # the browser's copy is current

    @app.post("/background/remove")
    async def remove_picture(request: Request) -> Response:
        """Back to the built-in pictures. Only the private copies go, by their fixed names, and only plain files or
        links: a folder under one of the names is left alone and said so."""
        left = False
        for name in (*NAMES, CREDIT):
            try:
                (folder / name).unlink(missing_ok=True)
            except OSError:  # a folder under the name, no permission, or no background folder at all
                left = True
        return RedirectResponse(f"/settings?look={'left' if left else 'removed'}#look", 303)


def of(folder: Path) -> Own:
    """The own picture as the pages see it now, so a file placed or removed counts at once. Every page costs only
    the files' sizes and times; their contents are read again when one of those changed."""
    key = tuple(_stamp(folder / name) for name in (*NAMES, CREDIT))
    seen = _seen.get(folder)
    if seen is None or seen[0] != key:
        _seen[folder] = seen = key, _look(folder)
    return seen[1]


def _stamp(path: Path) -> tuple | None:
    try:
        found = path.lstat()
    except OSError:
        return None
    return found.st_ino, found.st_mtime_ns, found.st_size, found.st_mode


def _look(folder: Path) -> Own:
    found = _first(folder)
    if found is None:
        return Own()
    path, opened = found
    if opened is None:
        return Own(problem=NOT_A_PICTURE.format(path.name))
    file, _ = opened
    try:
        kind = _kind(file.read(12))
    except OSError:  # a disk that fails mid-read: a problem, not a page that cannot be drawn
        kind = None
    finally:
        file.close()
    return Own(in_use=True, credit=_credit(folder)) if kind else Own(problem=NOT_A_PICTURE.format(path.name))


def _first(folder: Path) -> tuple[Path, tuple[int, os.stat_result] | None] | None:
    """The first own-picture name that exists, with the file open (None when it is refused); None when there is
    none, or no background folder."""
    for name in NAMES:
        path = folder / name
        try:
            return path, _open(path)
        except FileNotFoundError:
            continue
    return None


def _open(path: Path) -> tuple[io.FileIO, os.stat_result] | None:
    """The file opened once, with its details read from that open handle: None when it is not a plain file of at
    most MAX_BYTES (a link is not followed, a pipe does not block, a folder is refused, no permission). Raises
    FileNotFoundError when nothing is at the name, or the folder is not a folder. The handle closes itself if a
    reader drops it (an error before the first piece is sent), so none is ever left open."""
    try:
        file = io.FileIO(os.open(path, OPEN_FLAGS))
    except NotADirectoryError:
        raise FileNotFoundError(path.name) from None
    except FileNotFoundError:
        raise
    except OSError:
        return None
    details = os.fstat(file.fileno())
    if not stat.S_ISREG(details.st_mode) or details.st_size > MAX_BYTES:
        file.close()
        return None
    return file, details


def _chunks(file: io.FileIO, size: int) -> Iterator[bytes]:
    """The open file in pieces, never more than the size that was checked; closed however the sending ends."""
    try:
        while size > 0 and (piece := file.read(min(CHUNK, size))):
            size -= len(piece)
            yield piece
    finally:
        file.close()


def _kind(head: bytes) -> str | None:
    """The picture type from the file's first bytes, never from its name."""
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def _credit(folder: Path) -> str:
    """The credit file's first line, printable characters only, cut to MAX_CREDIT; the default when the file is
    missing, empty, or not a plain file."""
    try:
        opened = _open(folder / CREDIT)
    except FileNotFoundError:
        return DEFAULT_CREDIT
    if opened is None:
        return DEFAULT_CREDIT
    file, _ = opened
    try:
        text = file.read(MAX_CREDIT * 4).decode("utf-8", errors="replace")
    finally:
        file.close()
    first = text.partition("\n")[0]
    return "".join(char for char in first if char.isprintable()).strip()[:MAX_CREDIT] or DEFAULT_CREDIT
