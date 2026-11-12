"""The person's own background picture: a private file in the data folder, shown behind every signed-in page and
served to the signed-in person only. It never enters the repository; a missing file means the built-in pictures.
A picture uploaded on Settings is decoded, straightened, shrunk and rewritten without any hidden information."""

import contextlib
import io
import os
import stat
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

try:
    from PIL import Image, ImageOps
except ImportError:  # an install from before Pillow was pinned: the dashboard runs, uploading waits (Settings says)
    Image = ImageOps = None

FOLDER = "background"  # inside the data folder
# fixed names; the first found is used, the type comes from its bytes. An upload writes the first, so it always wins
# over a file placed by hand (which it also removes).
NAMES = ("own.webp", "own.jpg", "own.png")
PHONE = "own-phone.webp"  # the upload's portrait crop, used while it is as new as the main picture
CREDIT = "own.txt"  # the credit line, typed by the person; its first line is shown
PARTIAL = ".own-*.partial"  # a copy being written, beside the names, until it is moved onto one of them
ROUTE = "/background/own"  # named once in the stylesheet, so it never changes with the file
PHONE_ROUTE = "/background/own-phone"
UPLOAD = "/background/upload"  # the one address main.py reads a file-upload form for
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
# What an upload may be: only these decoders are asked (the JPEG one also answers a camera JPEG with a second picture
# inside, as "MPO"; its first picture is used), a picture is refused by its size before any pixel is decoded, and the
# decode itself is the real check of the bytes. 40 million pixels takes an 8K wallpaper (33 million) and keeps the
# decoded picture, 4 bytes a pixel in Pillow for RGB and RGBA alike, at 160 MB at the worst (see `_reduced`).
FORMATS = ["JPEG", "PNG", "WEBP"]
MAX_SIDE = 12000
MAX_PIXELS = 40_000_000
if Image:
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS  # Pillow's own bomb check, set to the same line as the explicit one below
LONG_SIDE = 2560  # the stored picture: no bigger than this on its long side, never enlarged
PHONE_SIZE = (1080, 1920)  # the phone copy: the middle of the picture, portrait, up to this
ROWS = 240  # a strip of a big picture: whole rows of every reduction, which is 4 at most (12000 / 2560)
FLOOR = (14, 23, 28)  # behind a see-through picture: the dark floor the pages use with an own picture
QUALITY = 82
_seen: dict[Path, tuple[tuple, "Own"]] = {}  # per folder: the files' sizes and times last looked at, and the answer


@dataclass(frozen=True)
class Own:
    """What the pages need: whether an own picture is in use, its words colour and credit, or why it is not; and
    whether this install can take an upload at all."""

    in_use: bool = False
    ink: str = INK
    credit: str = DEFAULT_CREDIT
    problem: str | None = None
    can_upload: bool = field(default_factory=lambda: Image is not None)


class Found(NamedTuple):
    """One of the fixed names, present. `file` is open at its first byte when it is a plain file within the cap
    that holds a picture of an allowed kind (`kind` says which); otherwise it is None and nothing is left open."""

    path: Path
    file: io.FileIO | None
    details: os.stat_result | None
    kind: str | None

    def close(self) -> None:
        if self.file:
            self.file.close()


def add_routes(app: FastAPI, data_dir: Path) -> None:
    folder = data_dir / FOLDER
    app.state.background = folder  # read by main._page for every page
    app.state.background_busy = threading.Lock()  # one picture is prepared at a time

    @app.get(ROUTE)
    async def own_picture(request: Request) -> Response:
        return _send(request, _first(folder, NAMES))

    @app.get(PHONE_ROUTE)
    async def own_phone_picture(request: Request) -> Response:
        """The phone copy while it is as new as the main picture and sound; else the main picture itself."""
        main, phone = _first(folder, NAMES), _first(folder, (PHONE,))
        if phone and phone.kind and (not (main and main.kind) or phone.details.st_mtime_ns >= main.details.st_mtime_ns):
            main and main.close()
            return _send(request, phone)
        phone and phone.close()
        return _send(request, main)

    @app.post(UPLOAD)
    async def upload_picture(request: Request) -> Response:
        """A picture from the person's computer becomes the private background. Its bytes are capped before any
        decoder sees them, prepared beside the dashboard's own thread, and nothing about them is ever told."""
        data = getattr(request.state, "files", {}).get("picture", b"")
        credit = _clean(request.state.form.get("credit", ""))
        if Image is None:
            outcome = "missing"
        elif not data or len(data) > MAX_BYTES:
            outcome = "refused"
        elif not request.app.state.background_busy.acquire(blocking=False):
            outcome = "busy"
        else:
            try:
                outcome = await run_in_threadpool(_store, folder, data, credit)
            finally:
                request.app.state.background_busy.release()
        return RedirectResponse(f"/settings?look={outcome}#look", 303)

    @app.post("/background/remove")
    async def remove_picture(request: Request) -> Response:
        """Back to the built-in pictures. Only the private copies go, by their fixed names, and only plain files or
        links: a folder under one of the names is left alone and said so. Never while an upload is being written."""
        if not request.app.state.background_busy.acquire(blocking=False):
            return RedirectResponse("/settings?look=busy#look", 303)
        try:
            left = False
            for name in (*NAMES, PHONE, CREDIT):
                try:
                    (folder / name).unlink(missing_ok=True)
                except OSError:  # a folder under the name, no permission, or no background folder at all
                    left = True
            _sweep(folder)
        finally:
            request.app.state.background_busy.release()
        return RedirectResponse(f"/settings?look={'left' if left else 'removed'}#look", 303)


def _send(request: Request, found: Found | None) -> Response:
    """The found file as the picture type its bytes say; 404 when there is none or it is refused, so the floor colour
    shows. The file was opened once: what was checked is what is sent, whatever happens to the name meanwhile."""
    if found is None or found.kind is None:
        raise HTTPException(404)
    details = found.details
    tag = f'"{details.st_ino:x}-{details.st_mtime_ns:x}-{details.st_size:x}"'
    if request.headers.get("if-none-match") == tag:  # the browser's copy is current
        found.close()
        return Response(status_code=304, headers={"ETag": tag})
    headers = {"ETag": tag, "Content-Length": str(details.st_size)}
    return StreamingResponse(_chunks(found.file, details.st_size), media_type=found.kind, headers=headers)


def of(folder: Path) -> Own:
    """The own picture as the pages see it now, so a file placed or removed counts at once. Every page costs only
    the files' sizes and times; their contents are read again when one of those changed."""
    key = tuple(_stamp(folder / name) for name in (*NAMES, PHONE, CREDIT))
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
    found = _first(folder, NAMES)
    if found is None:
        return Own()
    found.close()
    if found.kind is None:
        return Own(problem=NOT_A_PICTURE.format(found.path.name))
    return Own(in_use=True, credit=_credit(folder))


def _first(folder: Path, names: tuple[str, ...]) -> Found | None:
    """The first of these names that exists, opened once and judged by its first bytes; None when there is none, or
    no background folder."""
    for name in names:
        path = folder / name
        try:
            opened = _open(path)
        except FileNotFoundError:
            continue
        if opened is None:
            return Found(path, None, None, None)
        file, details = opened
        try:
            kind = _kind(file.read(12))
            file.seek(0)
        except OSError:  # a disk that fails mid-read: refused, not a page that cannot be drawn
            kind = None
        except BaseException:
            file.close()
            raise
        if kind is None:
            file.close()
            return Found(path, None, details, None)
        return Found(path, file, details, kind)
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
    """The credit file's first line, cleaned; the default when the file is missing, empty, or not a plain file."""
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
    return _clean(text)


def _clean(text: str) -> str:
    """One line of printable characters, at most MAX_CREDIT; the default for nothing."""
    first = text.partition("\n")[0]
    return "".join(char for char in first if char.isprintable()).strip()[:MAX_CREDIT] or DEFAULT_CREDIT


def _store(folder: Path, data: bytes, credit: str) -> str:
    """The uploaded bytes as the private background: "uploaded", "refused" (not a picture the app takes), "failed"
    (the folder could not be written) or "partly" (the picture is in place, its phone copy or credit is not). Every
    file is made in memory first, then all are written; nothing from a failure is kept or told."""
    try:
        pictures = _prepare(data)
    except Exception:  # whatever a crafted file makes a decoder raise: the one refusal, nothing of it told
        return "refused"
    if pictures is None:
        return "refused"
    files = dict(zip((NAMES[0], PHONE, CREDIT), (*pictures, f"{credit}\n".encode()), strict=True))
    return _write_all(folder, files)


def _prepare(data: bytes) -> tuple[bytes, bytes] | None:
    """The picture and its phone copy as WebP bytes; None when the bytes are not a picture within the limits."""
    with Image.open(io.BytesIO(data), formats=FORMATS) as opened:  # the header only, so far
        width, height = opened.size
        if not (0 < width <= MAX_SIDE and 0 < height <= MAX_SIDE and width * height <= MAX_PIXELS):
            return None
        if (longest := max(width, height)) > LONG_SIDE:  # a JPEG decodes at a reduced scale that still covers it
            opened.draft("RGB", (-(-width * LONG_SIDE // longest), -(-height * LONG_SIDE // longest)))
        opened.load()  # the whole first picture: the real check of the bytes; a later frame is never asked for
        ImageOps.exif_transpose(opened, in_place=True)  # as the camera meant it
        picture = _reduced(opened)
        picture.thumbnail((LONG_SIDE, LONG_SIDE), Image.Resampling.LANCZOS)
        if picture.mode == "RGBA":  # see-through parts onto the pages' dark floor, as the browser would not
            floor = Image.new("RGB", picture.size, FLOOR)
            floor.paste(picture, mask=picture.getchannel("A"))
            picture = floor
        box = (min(PHONE_SIZE[0], picture.width), min(PHONE_SIZE[1], picture.height))
        phone = ImageOps.fit(picture, box, Image.Resampling.LANCZOS)
        return _webp(picture), _webp(phone)


def _reduced(opened: "Image.Image") -> "Image.Image":
    """The decoded picture in true colours (RGBA when any part is see-through, else RGB), reduced by a whole number
    so its long side is at least LONG_SIDE and under twice that, cheaply: Pillow premultiplies a see-through picture
    at full size before it resamples, a second copy as big as the first (and shrinks a palette picture by nearest
    pixel), so a big picture is converted and box-reduced strip by strip, each strip a small copy. Measured on an
    8000x5000 PNG (the 40-million-pixel worst case), the whole preparation adds about 200 MB for an RGB picture,
    220 MB for a see-through one and 105 MB for a palette one; a JPEG, decoded reduced, 80 MB. Shrinking the
    see-through one whole took 370 MB, and compositing it at full size, as before, about 500 MB."""
    mode = "RGBA" if opened.has_transparency_data else "RGB"
    width, height = opened.size
    if (factor := max(width, height) // LONG_SIDE) < 2:
        return opened if opened.mode == mode else opened.convert(mode)
    picture = Image.new(mode, (-(-width // factor), -(-height // factor)))
    for top in range(0, height, ROWS):
        strip = opened.crop((0, top, width, min(top + ROWS, height))).convert(mode)
        picture.paste(strip.reduce(factor), (0, top // factor))
    return picture


def _webp(picture: "Image.Image") -> bytes:
    """The picture as WebP with nothing but its pixels. The WebP saver writes camera data, a colour profile or XMP
    only when they are passed to save, and none is; `info` is emptied too, as the JPEG and PNG savers would copy
    from it, so a change of output format could not quietly carry anything over."""
    picture.info.clear()
    out = io.BytesIO()
    picture.save(out, "WEBP", quality=QUALITY, method=4)
    return out.getvalue()


def _write_all(folder: Path, files: dict[str, bytes]) -> str:
    """Every file written to a temporary name in the folder first, then each moved onto its fixed name, the main
    picture first: "uploaded", or "failed" with the old set left, or "partly" when the new main picture is in place
    with the older phone copy (which the phone route then ignores) or credit, so Settings can say so. A file placed
    by hand under another name is removed once the upload is in place."""
    temps: list[tuple[str, str]] = []
    placed = 0
    try:
        folder.mkdir(mode=0o700, exist_ok=True)
        _sweep(folder)
        for name, content in files.items():
            handle, temp = tempfile.mkstemp(dir=folder, prefix=PARTIAL[:5], suffix=PARTIAL[6:])
            temps.append((temp, name))
            with os.fdopen(handle, "wb") as file:
                file.write(content)
        for temp, name in temps:
            os.replace(temp, folder / name)
            placed += 1
    except OSError:
        for temp, _ in temps[placed:]:
            with contextlib.suppress(OSError):
                os.unlink(temp)
        return "partly" if placed else "failed"
    for name in NAMES[1:]:
        with contextlib.suppress(OSError):
            (folder / name).unlink(missing_ok=True)
    return "uploaded"


def _sweep(folder: Path) -> None:
    """Copies left behind by a cut-off write: plain files under the temporary pattern, removed."""
    with contextlib.suppress(OSError):
        for path in folder.glob(PARTIAL):
            if stat.S_ISREG(path.lstat().st_mode):
                with contextlib.suppress(OSError):
                    path.unlink()
