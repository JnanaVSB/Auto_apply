"""The files job scoring downloads once: the meaning model, its word-piece list and the O*NET skills list.

They live in this computer's cache folder, outside the project and the data folder, can be downloaded again at any
time, and are only used when their size and SHA-256 fingerprint match the values pinned here.
"""

import hashlib
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.fetch import Fetcher, FetchError

MODEL_COMMIT = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"  # BAAI/bge-small-en-v1.5 (MIT), last changed 2024-02-22
HUGGING_FACE = f"https://huggingface.co/BAAI/bge-small-en-v1.5/resolve/{MODEL_COMMIT}"


@dataclass(frozen=True)
class SetupFile:
    name: str  # in the cache folder
    label: str  # in messages
    url: str
    size: int
    sha256: str


MODEL = SetupFile(
    "bge-small-en-v1.5.onnx",
    "the meaning model",
    f"{HUGGING_FACE}/onnx/model.onnx",
    133_093_490,
    "828e1496d7fabb79cfa4dcd84fa38625c0d3d21da474a00f08db0f559940cf35",
)
TOKENIZER = SetupFile(
    "bge-small-en-v1.5-tokenizer.json",
    "the meaning model's word-piece list",
    f"{HUGGING_FACE}/tokenizer.json",
    711_396,
    "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
)
SKILLS = SetupFile(  # O*NET 31.0 Software Skills, CC BY 4.0 (credited in the README)
    "onet-31.0-software-skills.csv",
    "the O*NET skills list",
    "https://www.onetcenter.org/dl_files/database/db_31_0_csv/software_skills.csv",
    3_557_545,
    "6aabb96b464288db849580e6510530efff3f80e25dde2bb23f1fc36bb526b016",
)
FILES = (MODEL, TOKENIZER, SKILLS)


class SetupError(Exception):
    """A scoring file is missing and could not be downloaded or saved. Messages never include a full path."""


def cache_dir() -> Path:
    """The app's folder in this system's standard cache location."""
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "auto_apply" / "Cache"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "auto_apply"
    xdg = Path(os.environ.get("XDG_CACHE_HOME", ""))
    return (xdg if xdg.is_absolute() else Path.home() / ".cache") / "auto_apply"  # a relative setting is ignored


def ensure_files(fetcher: Fetcher, out: Callable[[str], None] = print, folder: Path | None = None) -> Path:
    """Make sure every scoring file is in the cache folder and correct, downloading what is missing or damaged.

    Returns the folder. Safe to run again: correct files are never downloaded twice.
    """
    folder = folder or cache_dir()
    for file in FILES:
        try:
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not _correct(folder / file.name, file):
                out(f"Downloading {file.label} ({file.size / 1_000_000:.1f} MB), only this once...")
                fetcher.download_file(file.url, folder / file.name, file.size, file.sha256)
        except FetchError as error:
            raise SetupError(f"Could not download {file.label}: {error}.") from None
        except OSError as error:  # disk full, no permission, a file where the folder should be, ...
            raise SetupError(
                f"Could not save {file.label} in the app's cache folder ({error.strerror or type(error).__name__}). "
                "Free some disk space, or check that the cache folder can be written."
            ) from None
    return folder


def _correct(path: Path, file: SetupFile) -> bool:
    try:
        if path.stat().st_size != file.size:
            return False
        with path.open("rb") as data:
            return hashlib.file_digest(data, "sha256").hexdigest() == file.sha256
    except FileNotFoundError:
        return False
