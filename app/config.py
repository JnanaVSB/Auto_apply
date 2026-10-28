"""Load and validate the user's private settings from data/config.yaml."""

from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.sources import BOARD_NAME
from app.sources.boards import BOARDS

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_DIR / "data" / "config.yaml"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
DEFAULT_ADDRESSES = {"ollama": "http://127.0.0.1:11434", "anthropic": "https://api.anthropic.com"}


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


class ScoringSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # Stage A passes a job with one of your keywords in its title, or with both of these reached (0 to 1).
    meaning_threshold: Annotated[float, Field(ge=0, le=1)] = 0.5
    skills_threshold: Annotated[float, Field(ge=0, le=1)] = 0.15  # share of the job's skills your resume has
    extra_skills: list[str] = []  # tool names the O*NET skills list lacks, looked for in your resume and in job ads
    minutes_per_day: Annotated[int, Field(ge=1, le=1440, strict=True)] = 120  # automatic AI scoring time per day
    ready_threshold: Annotated[int, Field(ge=0, le=100, strict=True)] = 70  # AI score from which a job is ready
    automatic: bool = True  # while the dashboard is open, score whenever there is something to do


class AIRole(BaseModel):
    """One role's AI model (the Evaluator now; the Writer and the council later). Never holds a key itself."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    service: Literal["ollama", "openai-compatible", "anthropic"] = "ollama"
    model: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")]
    address: str = ""  # Ollama and Anthropic have defaults; an OpenAI-compatible service needs its own
    key_name: Annotated[str, Field(pattern=r"^[a-z0-9_-]{1,64}$")] | None = None  # the key's name in the keychain
    thinking: Literal["off", "low", "medium", "high"] = "off"
    context: Annotated[int, Field(ge=2048, le=262144, strict=True)] = 16384  # Ollama's working size, in word-pieces

    @model_validator(mode="before")
    @classmethod
    def _default_address(cls, values: object) -> object:
        if isinstance(values, dict) and not values.get("address"):
            return values | {"address": DEFAULT_ADDRESSES.get(values.get("service", "ollama"), "")}
        return values

    @model_validator(mode="after")
    def _safe_address(self) -> "AIRole":
        parts = urlsplit(self.address)
        if self.service == "ollama":
            if parts.scheme != "http" or parts.hostname not in LOCAL_HOSTS or parts.username or parts.password:
                raise ValueError(
                    "Ollama must run on this computer (address http://127.0.0.1:11434): it has no password, so it "
                    "must never be reachable from elsewhere"
                )
        elif parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise ValueError("a cloud service's address must start with https:// and hold no name or password")
        elif self.key_name is None:
            raise ValueError("a cloud service needs key_name: the name its key is saved under in the keychain")
        return self


class AISettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evaluator: AIRole = AIRole(model="gemma4:26b-a4b-it-q8_0")  # scores jobs (Stage B)


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    data_dir: Path
    roles: RoleSettings = RoleSettings()  # optional, so older settings files still work for app.db
    sources: SourceSettings = SourceSettings()
    collect_every_hours: Annotated[int, Field(ge=0, le=168, strict=True)] = 0  # dashboard schedule; 0 = off
    phonebook_every_days: Annotated[int, Field(ge=0, le=365, strict=True)] = 30  # dashboard schedule; 0 = off
    requests_per_second: Annotated[int, Field(ge=1, le=5, strict=True)] = 1  # to each job site; 1 is polite
    scoring: ScoringSettings = ScoringSettings()
    ai: AISettings = AISettings()


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
