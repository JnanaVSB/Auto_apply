"""The resume PDF, built on this computer by Tectonic from the app's own LaTeX: in a new private folder, with its
"untrusted input" setting on, no shell, and a time limit. The finished PDF is moved into place all at once."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path

BUNDLE = "https://data1b.fullyjustified.net/tlextras-2022.0r0.tar"  # given explicitly: an update can't change it
INSTALL = "conda install -n auto_apply -c conda-forge tectonic=0.17.0=*_0"
TIMEOUT_SECONDS = 300  # the first build downloads its LaTeX files (about 50 s); later ones take about a second
BUILDING = "building-"  # the private folders of builds in progress
LEFT_OVER_SECONDS = TIMEOUT_SECONDS + 60  # a build folder older than this was interrupted; a newer one may be busy
FAILED = "failed-build"  # the generated LaTeX and the log of the last failed build, kept for checking by hand
PAGES = re.compile(r"Output written on .*?\((\d+) pages?")
MISSING = re.compile(r"Missing character: There is no (.) ")
LATEX_ERROR = re.compile(r"^error: [^\n]*?\.tex:(\d+): (.{1,100})", re.M)
NO_DOWNLOAD = re.compile(r"download|connect|network|resolve|fullyjustified|https?://", re.I)
NO_NAME = "Fill in the Questionnaire's Contact section first: the PDF's header comes from there."
NEW_FILE = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)  # created readable by you only
_building = threading.Lock()  # one build at a time


class PdfError(Exception):
    """A PDF that couldn't be built. Messages never include resume text."""


@dataclass(frozen=True)
class Built:
    pages: int
    missing: tuple[str, ...]  # characters the font can't show, so left out of the PDF


def tectonic() -> Path | None:
    """Tectonic as installed in the app's conda environment (Linux and macOS, or Windows), else on the PATH."""
    for path in (Path(sys.prefix) / "bin" / "tectonic", Path(sys.prefix) / "Library" / "bin" / "tectonic.exe"):
        if path.is_file():
            return path
    found = shutil.which("tectonic")
    return Path(found) if found else None


def master(data_dir: Path, set_id: int) -> Path:
    """The master PDF of a confirmed tile set: all its tiles in your original wording."""
    return data_dir / "documents" / "master" / f"resume-v{set_id}.pdf"


def build_master(data_dir: Path, set_id: int, latex: str) -> Built:
    target = master(data_dir, set_id)
    try:
        target.parent.parent.mkdir(mode=0o700, exist_ok=True)  # documents/, readable by you only
    except OSError as error:
        raise PdfError(f"Could not make the documents folder in {data_dir} ({error.strerror}).") from None
    built = build(latex, target)
    _write(target.with_suffix(".json"), json.dumps(asdict(built), ensure_ascii=False).encode())
    return built


def built(target: Path) -> Built | None:
    """What the last good build of this PDF found (its pages, the characters left out); None if not built yet."""
    try:
        found = json.loads(target.with_suffix(".json").read_text(encoding="utf-8"))
        return Built(int(found["pages"]), tuple(found["missing"])) if target.is_file() else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def build(latex: str, target: Path, program: Path | None = None) -> Built:
    """Build the PDF at target from the LaTeX. A failed build keeps the LaTeX and the log in target's folder."""
    if not _building.acquire(blocking=False):
        raise PdfError("A PDF is being built; try again in a moment.")
    try:
        return _build(latex, target, program or tectonic())
    finally:
        _building.release()


def _build(latex: str, target: Path, program: Path | None) -> Built:
    if program is None:
        raise PdfError(f"Tectonic isn't installed: install it with: {INSTALL}")
    folder = target.parent
    try:
        folder.mkdir(mode=0o700, exist_ok=True)
        now = time.time()
        for old in folder.glob(f"{BUILDING}*"):
            with suppress(FileNotFoundError):  # another dashboard's build may finish meanwhile
                if now - old.stat().st_mtime > LEFT_OVER_SECONDS:  # never a build another dashboard is running
                    shutil.rmtree(old, ignore_errors=True)
        work = Path(tempfile.mkdtemp(prefix=BUILDING, dir=folder))  # readable by you only
    except OSError as error:
        raise PdfError(
            f"Could not write in {folder} ({error.strerror}): check it is yours and the disk has room."
        ) from None
    source = work / "resume.tex"
    try:
        _write(source, latex.encode())
        try:
            done = subprocess.run(  # noqa: S603 (fixed arguments, no shell)
                [str(program), "--untrusted", "--bundle", BUNDLE, "--outdir", str(work), "--keep-logs",
                    "--chatter", "minimal", str(source)],
                cwd=work, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT_SECONDS,
            )  # fmt: skip
        except subprocess.TimeoutExpired:
            raise PdfError(f"The PDF build took over {TIMEOUT_SECONDS // 60} minutes and was stopped.") from None
        except OSError as error:
            raise PdfError(f"Tectonic could not be started ({error.strerror}): reinstall it with: {INSTALL}") from None
        log = _read(work / "resume.log")
        if done.returncode != 0 or not (work / "resume.pdf").is_file():
            raise _failed(done.stderr, work, folder)
        _write(target, (work / "resume.pdf").read_bytes())
        shutil.rmtree(folder / FAILED, ignore_errors=True)
        pages = PAGES.search(log)
        return Built(int(pages[1]) if pages else 0, tuple(dict.fromkeys(MISSING.findall(log))))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _failed(output: str, work: Path, folder: Path) -> PdfError:
    """The error to show: a LaTeX error's kind and line (the files are kept), or a download that failed."""
    if error := LATEX_ERROR.search(output):
        kept = folder / FAILED
        shutil.rmtree(kept, ignore_errors=True)
        shutil.copytree(work, kept, ignore=shutil.ignore_patterns("*.pdf"))  # keeps the private folder's 700
        return PdfError(
            f"The PDF could not be built: {error[2].strip()}, line {error[1]} of the generated file "
            f"(kept with its log in {kept})."
        )
    if NO_DOWNLOAD.search(output):
        return PdfError(
            f"Tectonic needs to download its LaTeX files once from {BUNDLE.split('/')[2]}: check your internet "
            "connection and try again. If it keeps failing, that address may have changed; an app update fixes it."
        )
    return PdfError(f"The PDF could not be built: Tectonic stopped without saying why. Check it runs: {INSTALL}")


def _write(path: Path, data: bytes) -> None:
    """All or nothing: written next to its place, then swapped in; readable by you only."""
    partial = path.with_name(f"{path.name}.partial")
    try:
        with open(os.open(partial, NEW_FILE, 0o600), "wb") as file:
            file.write(data)
        partial.replace(path)
    except OSError as error:
        partial.unlink(missing_ok=True)
        raise PdfError(f"Could not save {path.name} in {path.parent} ({error.strerror}).") from None


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
