"""Load and validate the user's private settings from data/config.yaml."""

from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.sources import BOARD_NAME
from app.sources.boards import BOARDS

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_DIR / "data" / "config.yaml"


class ConfigError(Exception):
    """Settings are missing or invalid. Messages name the setting but never include its value."""


def _github_file(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != "raw.githubusercontent.com" or parts.path.count("/") < 3:
        raise ValueError("must be an https://raw.githubusercontent.com/<owner>/<repo>/... address")
    return url


GitHubFile = Annotated[str, AfterValidator(_github_file)]  # a file in a public GitHub repository


class SimplifyList(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    url: GitHubFile
    level: Literal["intern", "new grad"]


class RoleSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    simplify_categories: list[str] = []
    keywords: list[str] = []


class SourceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    simplify_lists: list[SimplifyList] = []
    extra_boards: list[str] = []
    phonebook_lists: list[GitHubFile] = []  # other public GitHub job lists the phone book reads; you approve each

    @field_validator("extra_boards")
    @classmethod
    def _board_names(cls, boards: list[str]) -> list[str]:
        cleaned = [board.strip().lower() for board in boards]
        for board in cleaned:
            kind, _, name = board.partition(":")
            if kind not in BOARDS or not BOARD_NAME.fullmatch(name):
                *others, last = (f"{kind}:<name>" for kind in BOARDS)
                raise ValueError(f"each must look like {', '.join(others)} or {last}")
        return cleaned


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    data_dir: Path
    roles: RoleSettings = RoleSettings()  # optional, so older settings files still work for app.db
    sources: SourceSettings = SourceSettings()
    collect_every_hours: Annotated[int, Field(ge=0, le=168, strict=True)] = 0  # dashboard schedule; 0 = off
    phonebook_every_days: Annotated[int, Field(ge=0, le=365, strict=True)] = 30  # dashboard schedule; 0 = off
    requests_per_second: Annotated[int, Field(ge=1, le=5, strict=True)] = 1  # to each job site; 1 is polite


def load_config(path: Path | None = None) -> Config:
    path = path or CONFIG_PATH  # looked up when called, so every part of the app reads the same file
    if not path.is_file():
        raise ConfigError(
            "Settings file not found at data/config.yaml. Copy config.example.yaml to data/config.yaml and fill it in."
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        where = f" (line {mark.line + 1})" if mark else ""
        raise ConfigError(f"data/config.yaml is not valid YAML{where}.") from None
    try:
        config = Config.model_validate(raw)
    except ValidationError as error:
        problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'settings'}: {e['msg']}" for e in error.errors())
        raise ConfigError(f"Invalid settings in data/config.yaml: {problems}") from None
    data_dir = (PROJECT_DIR / config.data_dir.expanduser()).resolve()
    if PROJECT_DIR.is_relative_to(data_dir) or Path.home().resolve().is_relative_to(data_dir):
        raise ConfigError(
            "Invalid settings in data/config.yaml: data_dir must be a folder of its own (for example: data), "
            "not the project folder, your home folder, or a folder above them."
        )
    return config.model_copy(update={"data_dir": data_dir})
