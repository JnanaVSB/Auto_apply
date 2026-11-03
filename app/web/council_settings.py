"""The council page (/settings/council), kept apart from main.py: the presets that fill in the Writer and the five
reviewers, each member's model and estimated cost, warnings, how the council works (rounds, discussion turns,
automatic preparation, council hours, the daily job cap), and the members you add, whose instructions are kept only in
your data folder (data/skills/<id>.md, never in the repository)."""

import os
from collections.abc import Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException

from app.config import OPENAI_HOST, REVIEWERS, AIRole, Config, ConfigError, load_config
from app.council import automatic, estimate, reviewers
from app.resume import CONTROL
from app.web import actions
from app.web.writer_settings import OPENAI_ADDRESS, SWAP, WHOLE, saved_keys

PAGE = "/settings/council"
GEMMA, QWEN = "gemma4:26b-a4b-it-q8_0", "qwen3.6:35b-a3b"
ADDED = ("x1", "x2", "x3")
MAX_INSTRUCTIONS = 20_000  # characters


def _local(model: str) -> dict:
    return {"service": "ollama", "model": model, "thinking": "off", "context": 32768}  # owner choice Q7-1


def _claude(model: str, thinking: str) -> dict:
    return {"service": "anthropic", "model": f"claude-{model}-5-5", "key_name": "anthropic", "thinking": thinking}


def _gpt(model: str, thinking: str) -> dict:
    return {"service": "openai-compatible", "model": f"gpt-6.1-{model}", "address": OPENAI_ADDRESS,
            "key_name": "openai", "thinking": thinking}  # fmt: skip


MEMBERS = ("writer", *REVIEWERS)
PRESETS = {  # Writer, Truth checker, Hiring manager, Writing editor, ATS screener, Domain expert (owner, 2026-11-01)
    "laptop": (_local(GEMMA), _local(QWEN), _local(QWEN), _local(QWEN), _local(GEMMA), _local(QWEN)),
    "low": (_claude("sonnet", "medium"), _gpt("sol", "medium"), _gpt("terra", "medium"), _gpt("terra", "medium"),
            _claude("haiku", "medium"), _gpt("terra", "medium")),
    "recommended": (_claude("opus", "high"), _gpt("sol", "high"), _claude("sonnet", "high"), _gpt("sol", "high"),
                    _gpt("sol", "high"), _claude("sonnet", "high")),
    "high": (_claude("opus", "xhigh"), _gpt("sol", "xhigh"), _claude("sonnet", "high"), _claude("opus", "high"),
             _gpt("sol", "high"), _claude("sonnet", "high")),
}  # fmt: skip
NAMES = {"laptop": "Laptop", "low": "Low", "recommended": "Recommended", "high": "High"}
SAVED = {"preset": "Preset applied.", "council": "Saved.", "member": "Member saved.", "removed": "Member removed."}
SAME = ("The Truth checker uses the same {} as the Writer: it may go easier on the Writer's work. A different one is "
        "safer.")  # fmt: skip


def add_routes(app: FastAPI, page: Callable[..., HTMLResponse]) -> None:
    @app.get(PAGE)
    async def council_page(request: Request) -> Response:
        return _view(request, page)

    @app.post(f"{PAGE}/preset")
    async def apply_preset(request: Request) -> Response:
        name = request.state.form.get("preset", "")
        if name not in PRESETS:
            raise HTTPException(404)
        roles = dict(zip(MEMBERS, PRESETS[name], strict=True))
        return _saved(
            request, page, "preset", lambda old: old | {"ai": dict(old.get("ai") or {}) | roles | {"tier": name}}
        )

    @app.post(PAGE)
    async def save_council(request: Request) -> Response:
        form = request.state.form
        council = {name: _whole(form.get(name, "")) for name in ("rounds", "turns", "daily_jobs")} | {
            "start": form.get("start", "").strip(), "end": form.get("end", "").strip(),
            "automatic": form.get("automatic") == "on", "any_time": form.get("any_time") == "on",
        }  # fmt: skip
        if council["automatic"]:
            try:
                config = load_config()
                roles = [config.ai.writer, *config.ai.members.values()]  # the members you added too
                missing = None if None in roles else automatic.missing_key(roles)
            except ConfigError:
                missing = None
            if missing:
                return _view(request, page, problem=f"Not saved: automatic preparation needs the key '{missing}' "
                                                    "saved first (Writer settings, Keys).", typed=form)  # fmt: skip
        return _saved(request, page, "council", lambda old: old | {"council": council}, form)

    @app.post(f"{PAGE}/members")
    async def save_member(request: Request) -> Response:
        """Adds a member, or changes one you added: its name, whether it reviews letters, its instructions."""
        form, config = request.state.form, load_config()
        data_dir, extra = config.data_dir, list(config.ai.extra)
        member = form.get("id") or next((found for found in ADDED if found not in {m.id for m in extra}), None)
        if member is None:  # an id from the form is checked with the settings (x1 to x3) before any file is written
            return _view(request, page, problem="At most 3 members can be added.")
        text = CONTROL.sub("", form.get("instructions", "")).strip()
        if not text or len(text) > MAX_INSTRUCTIONS:
            return _view(request, page, problem=f"Write the member's instructions (at most {MAX_INSTRUCTIONS:,} "
                                                "characters).", typed=form)  # fmt: skip
        before = next((found for found in extra if found.id == member), None)
        added = {"id": member, "name": " ".join(form.get("name", "").split()), "letters": form.get("letters") == "on",
                 "role": before.role.model_dump(exclude_none=True) if before else _local(GEMMA)}  # fmt: skip

        def change(old: dict) -> dict:
            others = [found for found in (old.get("ai") or {}).get("extra", []) if found["id"] != member]
            return old | {"ai": dict(old.get("ai") or {}) | {"extra": [*others, added]}}

        if problem := actions.update_settings(change):
            return _view(request, page, problem=problem, typed=form)
        _write_instructions(data_dir, member, text)
        request.app.state.preparer.resume()
        return RedirectResponse(f"{PAGE}?saved=member", 303)

    @app.post(PAGE + "/members/{member}/remove")
    async def remove_member(request: Request, member: str) -> Response:
        """Stops a member you added from reviewing new versions; its earlier answers and instructions are kept."""
        if member not in ADDED:
            raise HTTPException(404)

        def change(old: dict) -> dict:
            ai = dict(old.get("ai") or {})
            return old | {"ai": ai | {"extra": [found for found in ai.get("extra", []) if found["id"] != member]}}

        return _saved(request, page, "removed", change)


def _saved(request: Request, page, done: str, change, typed: dict | None = None) -> Response:
    if problem := actions.update_settings(change):
        return _view(request, page, problem=problem, typed=typed)
    request.app.state.preparer.resume()  # a service paused for its settings is tried again
    return RedirectResponse(f"{PAGE}?saved={done}", 303)  # so reloading the page never saves twice


def _write_instructions(data_dir, member: str, text: str) -> None:
    """Your instructions for a member you added: in your data folder only, swapped in whole."""
    folder = data_dir / "skills"
    folder.mkdir(mode=0o700, exist_ok=True)
    new = folder / f"{member}.md.new"
    new.write_text(text + "\n", encoding="utf-8")
    new.chmod(0o600)
    os.replace(new, folder / f"{member}.md")


def _whole(text: str) -> int | str:
    text = text.strip()
    return int(text) if WHOLE.fullmatch(text) else text


def tier(config: Config) -> str:
    """The line-up's name: a preset as it is, "Custom (based on ...)" once changed, else "Custom" or "None chosen"."""
    roles = [config.ai.writer, *(getattr(config.ai, member) for member in REVIEWERS)]
    base = NAMES.get(config.ai.tier or "")
    if base and all(role and role.model_dump() == AIRole(**preset).model_dump()
                    for role, preset in zip(roles, PRESETS[config.ai.tier], strict=True)):  # fmt: skip
        return base
    if all(role is None for role in roles):
        return "None chosen"
    return f"Custom (based on {base})" if base else "Custom"


def _company(role: AIRole) -> str:
    if role.service == "ollama":
        return f"laptop model ({role.model})"
    if role.service == "anthropic":
        return "company (Anthropic)"
    host = urlsplit(role.address).hostname
    return "company (OpenAI)" if host == OPENAI_HOST else f"address ({host})"


def _view(request: Request, page, problem: str | None = None, typed: dict | None = None) -> HTMLResponse:
    try:
        config = load_config()
    except ConfigError as error:
        return page(request, "council_settings.html", file_problem=str(error))
    keys, council = saved_keys(), config.council
    names, roles = config.ai.names, {"writer": config.ai.writer} | config.ai.members
    conn = request.app.state.conn
    rows = [{
        "member": member, "name": "Writer" if member == "writer" else reviewers.name(member, names), "role": role,
        "cost": role and estimate.member(conn, member, role, council.rounds, council.turns, half=True),
        "letters": member in reviewers.LETTER_MEMBERS or any(a.id == member and a.letters for a in config.ai.extra),
    } for member, role in roles.items()]  # fmt: skip
    writer, truth, evaluator = config.ai.writer, config.ai.truth, config.ai.evaluator
    missing = sorted(
        {role.key_name for role in roles.values() if role and role.key_name and keys.get(role.key_name) is False}
    )
    switching = evaluator.service == "ollama" and any(
        role and role.service == "ollama" and (role.model, role.context) != (evaluator.model, evaluator.context)
        for role in roles.values()
    )
    warnings = [
        SAME.format(_company(truth)) if writer and truth and _company(writer) == _company(truth) else "",
        *(f"No key is saved under '{name}': add it on the Writer settings page." for name in missing),
        SWAP if switching else "",
    ]
    costs = [row["cost"] for row in rows]
    return page(
        request, "council_settings.html", rows=rows, tier=tier(config), presets=NAMES, council=council,
        total=None if None in costs else sum(costs), warnings=[line for line in warnings if line],
        extra=config.ai.extra, added=_instructions(config), problem=problem, typed=typed,
        saved=SAVED.get(request.query_params.get("saved", "")),
    )  # fmt: skip


def _instructions(config: Config) -> dict[str, str]:
    """Your instructions for each member you added, to show in its box."""
    found = {}
    for added in config.ai.extra:
        try:
            found[added.id] = reviewers.instructions(added.id, config.data_dir)
        except reviewers.NoInstructions:
            found[added.id] = ""
    return found
