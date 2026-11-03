"""The Writer page (/settings/writer), kept apart from main.py: which model writes your resumes, its keys, a free
test, the spending limits and the one-page switch. With ?member=, the same picker chooses a council member's model
(app/web/council_settings.py). Keys go only into the keychain: never shown back, never in a file."""

import re
from collections.abc import Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from app import llm
from app.config import OPENAI_HOST, AIRole, Config, ConfigError, load_config
from app.council import reviewers
from app.keychain import KeychainError, delete_secret, get_secret, set_secret
from app.llm import ollama, prices
from app.web import actions

PAGE = "/settings/writer"
KEYS = {"anthropic": "Anthropic", "openai": "OpenAI", "own-address": "Your own address"}  # keychain name: shown name
KEY = re.compile(r"[\x21-\x7e]{1,500}")  # a key as services give them: visible characters, no spaces
NUMBER = re.compile(r"[0-9]{1,5}(\.[0-9]{1,6})?")  # a price or a limit; anything else is kept, so the check names it
WHOLE = re.compile(r"[0-9]{1,9}")
OPENAI_ADDRESS = f"https://{OPENAI_HOST}/v1"
OLLAMA = AIRole(model="any")  # only its address is used: to list the models Ollama on this computer has downloaded
SAVED = {"writer": "Writer saved.", "key": "Key saved.", "removed": "Key removed.", "limits": "Saved."}
SWAP = (
    "The Writer's local model or working size differs from the scorer's: Ollama will switch between them while "
    "scoring and writing, which can take a minute each time."
)
NOT_COUNTED = "Your own address has no price: its costs are not counted, so your spending limits can't protect you."


def add_routes(app: FastAPI, page: Callable[..., HTMLResponse]) -> None:
    @app.get(PAGE)
    async def writer_page(request: Request) -> Response:
        return await _view(request, page)

    @app.post(PAGE)
    async def choose_writer(request: Request) -> Response:
        form = request.state.form
        try:
            config = load_config()
        except ConfigError as error:
            return page(request, "writer_settings.html", file_problem=str(error))
        member = _member(request, config)
        role, problem = _role(form, _role_of(config, member), await run_in_threadpool(ollama.installed, OLLAMA))
        problem = problem or actions.update_settings(lambda old: old | {"ai": _with(old.get("ai") or {}, member, role)})
        if problem:
            return await _view(request, page, problem=problem, typed=form)
        request.app.state.preparer.resume()  # a paused service is tried again
        return RedirectResponse(f"{PAGE}?member={member}&saved=writer", 303)  # reloading never saves twice

    @app.post(f"{PAGE}/test")
    async def test_writer(request: Request) -> Response:
        try:
            config = load_config()
            role = _role_of(config, _member(request, config))
            result = (False, "Choose a model first.") if role is None else await run_in_threadpool(llm.check, role)
        except (ConfigError, KeychainError) as error:
            result = (False, str(error))
        return await _view(request, page, test=result)

    @app.post("/settings/keys/{name}")
    async def save_key(request: Request, name: str) -> Response:
        if name not in KEYS:
            raise HTTPException(404)
        form, remove = request.state.form, bool(request.state.form.get("remove"))
        if not remove and not KEY.fullmatch(form.get("key", "")):
            return await _view(request, page, problem="Not saved: paste the whole key, without spaces.")
        try:
            delete_secret(name) if remove else set_secret(name, form["key"])
        except KeychainError as error:
            return await _view(request, page, problem=str(error))
        request.app.state.preparer.resume()  # a service paused for its key is tried again
        return RedirectResponse(f"{PAGE}?saved={'removed' if remove else 'key'}", 303)

    @app.post("/settings/limits")
    async def save_limits(request: Request) -> Response:
        form = request.state.form
        spending = {name: _number(form.get(name, "")) for name in ("daily", "monthly")}
        one_page = {"one_page": form.get("one_page") == "on"}
        problem = actions.update_settings(
            lambda old: old | {"spending": spending, "documents": dict(old.get("documents") or {}) | one_page}
        )
        if problem:
            return await _view(request, page, problem=problem, typed=form)
        return RedirectResponse(f"{PAGE}?saved=limits", 303)


def _member(request: Request, config: Config) -> str:
    """Which member the page chooses a model for: the Writer, a reviewer, or a member you added (else not found)."""
    member = request.query_params.get("member") or getattr(request.state, "form", {}).get("member") or "writer"
    if member not in ("writer", *config.ai.members):
        raise HTTPException(404)
    return member


def _role_of(config: Config, member: str) -> AIRole | None:
    return config.ai.writer if member == "writer" else config.ai.members[member]


def _with(ai: dict, member: str, role: dict) -> dict:
    """The settings file's ai section with one member's model replaced."""
    if member.startswith("x"):
        return ai | {"extra": [added | ({"role": role} if added["id"] == member else {}) for added in ai["extra"]]}
    return ai | {member: role}


def _role(form: dict[str, str], current: AIRole | None, installed: list[str]) -> tuple[dict, str | None]:
    """The Writer's settings from one of the page's four forms, and why they can't be saved (or None)."""
    kind, model, thinking = form.get("kind"), form.get("model", "").strip(), form.get("thinking", "off")
    if kind == "local":
        if model not in installed:
            return {}, f"Choose a model Ollama has downloaded (run `ollama pull <model>` first): {model or 'none'}."
        return {
            "service": "ollama",
            "model": model,
            "thinking": thinking,
            "context": _whole(form.get("context", "")),
        }, None
    if kind in ("anthropic", "openai"):
        if model not in prices.models(kind):
            return {}, "Choose one of the listed models."
        if kind == "anthropic":
            return {"service": "anthropic", "model": model, "key_name": "anthropic", "thinking": thinking}, None
        return {"service": "openai-compatible", "model": model, "address": OPENAI_ADDRESS, "key_name": "openai",
                "thinking": thinking}, None  # fmt: skip
    if kind != "own":
        return {}, "Choose one of the four kinds of model."
    address = form.get("address", "").strip().rstrip("/")
    role = {"service": "openai-compatible", "model": model, "address": address, "thinking": thinking}
    role |= {"key_name": "own-address"} if form.get("needs_key") else {}
    role |= {name: _number(form[name]) for name in ("price_input", "price_output") if form.get(name, "").strip()}
    confirmed = current is not None and current.own and current.address == address
    if not confirmed and not form.get("confirm"):
        host = urlsplit(address).hostname or "this address"
        return role, f"Tick the box to confirm that your entries (contact details hidden) and job ads go to {host}."
    return role, None


def _number(text: str) -> float | str:
    text = text.strip()
    return float(text) if NUMBER.fullmatch(text) else text


def _whole(text: str) -> int | str:
    text = text.strip()
    return int(text) if WHOLE.fullmatch(text) else text


def _kind(role: AIRole) -> str:
    return {"ollama": "local", "anthropic": "anthropic"}.get(role.service) or ("own" if role.own else "openai")


def saved_keys() -> dict[str, bool | None]:
    """Whether each key is saved; None when the keychain can't be read."""
    try:
        return {name: get_secret(name) is not None for name in KEYS}
    except KeychainError:
        return dict.fromkeys(KEYS)


def _values(writer: AIRole | None, evaluator: AIRole, typed: dict[str, str] | None) -> dict[str, dict[str, str]]:
    """What each of the four forms shows: the Writer in use, or what was just typed, else a sensible start."""
    values = {
        "local": {"model": evaluator.model, "thinking": "off", "context": str(evaluator.context)},
        "anthropic": {"model": "claude-opus-5-5", "thinking": "high"},
        "openai": {"model": "gpt-6.1-sol", "thinking": "high"},
        "own": {"address": "", "model": "", "thinking": "off", "needs_key": "", "price_input": "", "price_output": ""},
    }
    if writer:
        role = writer.model_dump(exclude_none=True)
        values[_kind(writer)] |= {name: str(role[name]) for name in values[_kind(writer)] if name in role}
        values["own"]["needs_key"] = "on" if writer.own and writer.key_name else ""
    if typed and (kind := typed.get("kind")) in values:
        values[kind] |= {name: typed.get(name, "") for name in values[kind]}
    return values


async def _view(
    request: Request, page: Callable[..., HTMLResponse], problem: str | None = None,
    typed: dict[str, str] | None = None, test: tuple[bool, str] | None = None,
) -> HTMLResponse:  # fmt: skip
    try:
        config = load_config()
    except ConfigError as error:
        return page(request, "writer_settings.html", file_problem=str(error))
    member = _member(request, config)
    writer, evaluator, keys = _role_of(config, member), config.ai.evaluator, saved_keys()
    name = reviewers.name(member, config.ai.names) if member != "writer" else ""
    local_scorer = evaluator.service == "ollama"
    warnings = [
        SWAP if writer and writer.service == "ollama" and local_scorer
        and (writer.model, writer.context) != (evaluator.model, evaluator.context) else "",
        NOT_COUNTED if writer and writer.own and writer.price_input is None else "",
        f"No key is saved under '{writer.key_name}': add it in Keys below."
        if writer and writer.key_name and keys[writer.key_name] is False else "",
    ]  # fmt: skip
    return page(
        request, "writer_settings.html", writer=writer, kind=writer and _kind(writer), keys=keys, names=KEYS,
        member=member, member_name=name,
        values=_values(writer, evaluator, typed), installed=await run_in_threadpool(ollama.installed, OLLAMA),
        models={kind: prices.models(kind) for kind in ("anthropic", "openai")}, checked=prices.checked(),
        spending=config.spending, one_page=config.documents.one_page, warnings=[line for line in warnings if line],
        problem=problem, test=test, saved=SAVED.get(request.query_params.get("saved", "")),
        limits=typed if typed and "daily" in typed else None,
    )  # fmt: skip
