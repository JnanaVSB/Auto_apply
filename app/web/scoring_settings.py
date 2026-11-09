"""Settings → Scoring model (/settings/scoring-model), kept apart from main.py: which model gives jobs their AI score.
On this computer (Ollama, the default), or through OpenAI, Anthropic or your own address once you ticked the box for
exactly that address. OpenAI and Anthropic use the Writer page's key; your own address's key goes only into the
keychain, under a name tied to its host. A save writes the model and the ticked address together, or nothing."""

import hashlib
from collections.abc import Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app import llm
from app.config import DEFAULT_ADDRESSES, AISettings, ConfigError, load_config
from app.keychain import KeychainError, delete_secret, get_secret, set_secret
from app.llm import prices
from app.web import actions, security
from app.web.writer_settings import KEY, OPENAI_ADDRESS, number, saved_keys

PAGE = "/settings/scoring-model"
ON_THE_LAPTOP = "Change this key on the laptop."
ACCOUNT_ID = "YOUR-ACCOUNT-ID"
PRESETS = {  # each fills in only the address; you make your own account and key
    "groq": ("Groq", "https://api.groq.com/openai/v1"),
    "cloudflare": ("Cloudflare", f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/v1"),
    "openrouter": ("OpenRouter", "https://openrouter.ai/api/v1"),
}
PAID = {  # (shown name, service, address); the key is the Writer page's, under the same name as here
    "openai": ("OpenAI", "openai-compatible", OPENAI_ADDRESS),
    "anthropic": ("Anthropic", "anthropic", DEFAULT_ADDRESSES["anthropic"]),
}
SAVED = {
    "local": "Saved: jobs are scored on this computer.",
    "own": "Saved: jobs are scored through your own address.",
    "openai": "Saved: jobs are scored through OpenAI.",
    "anthropic": "Saved: jobs are scored through Anthropic.",
    "key": "Key saved.",
    "removed": "Key removed.",
}
OWN_FIELDS = ("address", "model", "needs_key", "price_input", "price_output")
UNTICKED = "Not saved: tick the box to confirm that your resume (contact details hidden) and job ads go to {}."


def add_routes(app: FastAPI, page: Callable[..., HTMLResponse]) -> None:
    @app.get(PAGE)
    async def scoring_page(request: Request) -> Response:
        return await _view(request, page)

    @app.post(f"{PAGE}/local")
    async def score_here(request: Request) -> Response:
        default = {"model": AISettings().evaluator.model}  # the local model's name is changed on main Settings
        return await _saved(request, page, default, "", "local")

    @app.post(f"{PAGE}/own")
    async def score_through_own_address(request: Request) -> Response:
        return await _cloud(request, page, *_own(request.state.form), "own")

    @app.post(f"{PAGE}/paid")
    async def score_through_openai_or_anthropic(request: Request) -> Response:
        return await _cloud(request, page, *_paid(request.state.form), request.state.form.get("kind", ""))

    @app.post(f"{PAGE}/test")
    async def test_scoring(request: Request) -> Response:
        try:
            result = await run_in_threadpool(llm.check, load_config().ai.evaluator)
        except (ConfigError, KeychainError) as error:
            result = (False, str(error))
        return await _view(request, page, test=result)

    @app.post(f"{PAGE}/key")
    async def save_key(request: Request) -> Response:
        security.laptop_only(request, ON_THE_LAPTOP)
        form, remove = request.state.form, bool(request.state.form.get("remove"))
        try:
            scorer = load_config().ai.evaluator
        except ConfigError as error:
            return page(request, "scoring_settings.html", file_problem=str(error))
        if not scorer.own:
            return await _view(request, page, problem="Not saved: save your own address first, then its key.")
        if form.get("host", "") != _host(scorer.address):  # a page opened for another address, or before a switch
            problem = "Not saved: the address changed since this page was opened; reload it, then save the key."
            return await _view(request, page, problem=problem)
        if not remove and not KEY.fullmatch(form.get("key", "")):
            return await _view(request, page, problem="Not saved: paste the whole key, without spaces.")
        name = key_name(_host(scorer.address))
        try:
            delete_secret(name) if remove else set_secret(name, form["key"])
        except KeychainError as error:
            return await _view(request, page, problem=str(error))
        return RedirectResponse(f"{PAGE}?saved={'removed' if remove else 'key'}", 303)


async def _saved(
    request: Request, page: Callable[..., HTMLResponse], role: dict, ticked: str, saved: str, problem: str | None = None
) -> Response:
    """The scoring model and the address you ticked for, written together; or nothing, and the form keeps what was
    typed."""

    def change(old: dict) -> dict:
        ai, scoring = dict(old.get("ai") or {}), dict(old.get("scoring") or {})
        return old | {"ai": ai | {"evaluator": role}, "scoring": scoring | {"cloud_ok": ticked}}

    if problem := problem or actions.update_settings(change):
        return await _view(request, page, problem=problem, typed=request.state.form)
    request.app.state.scorer.failed_at = None  # a run the old setting stopped may start again at once
    return RedirectResponse(f"{PAGE}?saved={saved}", 303)  # reloading never saves twice


async def _cloud(
    request: Request, page: Callable[..., HTMLResponse], role: dict, problem: str | None, saved: str
) -> Response:
    """A cloud scoring model: asked once per address (the tick), after the form's own problems, then written as _saved
    does."""
    try:
        cloud_ok = load_config().scoring.cloud_ok
    except ConfigError as error:
        return page(request, "scoring_settings.html", file_problem=str(error))
    if not problem and not request.state.form.get("confirm") and role["address"] != cloud_ok:
        problem = UNTICKED.format(_host(role["address"]) or "this address")
    return await _saved(request, page, role, role["address"], saved, problem)


def _own(form: dict[str, str]) -> tuple[dict, str | None]:
    """Your own address's scoring model from the form, and why it can't be saved (or None). The settings' own rules
    (https://, both prices or neither, a model name) are checked when it is saved."""
    address, model = form.get("address", "").strip().removesuffix("/"), form.get("model", "").strip()
    role = {"service": "openai-compatible", "model": model, "address": address, "thinking": "off"}
    role |= {"key_name": key_name(_host(address))} if form.get("needs_key") else {}
    role |= {name: number(form[name]) for name in ("price_input", "price_output") if form.get(name, "").strip()}
    if ACCOUNT_ID in address:
        return role, f"Not saved: put your own Cloudflare account id where the address says {ACCOUNT_ID}."
    if model.lower().startswith("openrouter/"):  # the scoring gate refuses it too
        return role, f"Not saved: the model {model} lets OpenRouter choose the model and who runs it: name one model."
    return role, None


def _paid(form: dict[str, str]) -> tuple[dict, str | None]:
    """OpenAI's or Anthropic's scoring model from the form (kind, model), with the Writer page's key, and why it can't
    be saved (or None). The address is written out, so the ticked one is exactly the one the scoring gate compares."""
    kind, model = form.get("kind", ""), form.get("model", "")
    _, service, address = PAID.get(kind, ("", "", ""))
    role = {"service": service, "model": model, "address": address, "key_name": kind, "thinking": "off"}
    if kind not in PAID or model not in prices.models(kind):
        return role, "Not saved: choose one of the listed models."
    return role, None


def key_name(host: str) -> str:
    """The keychain name of your own address's key, tied to its host: a key saved for one host never goes to another."""
    return f"own-scoring-{hashlib.sha256(host.encode()).hexdigest()[:16]}"


def _host(address: str) -> str:
    try:
        return urlsplit(address).hostname or ""
    except ValueError:  # a broken address, such as https://[x
        return ""


async def _view(
    request: Request, page: Callable[..., HTMLResponse], problem: str | None = None,
    typed: dict[str, str] | None = None, test: tuple[bool, str] | None = None,
) -> HTMLResponse:  # fmt: skip
    try:
        config = load_config()
    except ConfigError as error:
        return page(request, "scoring_settings.html", file_problem=str(error))
    scorer = config.ai.evaluator
    own = dict.fromkeys(OWN_FIELDS, "")
    if scorer.own:
        found = scorer.model_dump(exclude_none=True)
        own |= {name: str(found[name]) for name in OWN_FIELDS if name in found}
        own["needs_key"] = "on" if scorer.key_name else ""
    if preset := PRESETS.get(request.query_params.get("preset", "")):
        own["address"] = preset[1]
    keys = saved_keys()
    paid = {
        kind: {
            "label": label, "host": _host(address), "models": prices.models(kind), "key": keys[kind],
            "model": scorer.model if scorer.address == address else "", "ticked": address == config.scoring.cloud_ok,
        }
        for kind, (label, _, address) in PAID.items()
    }  # fmt: skip
    if typed and typed.get("kind") in paid:  # OpenAI's or Anthropic's form keeps the model chosen
        paid[typed["kind"]]["model"] = typed.get("model", "")
    elif typed:
        own |= {name: typed.get(name, "") for name in OWN_FIELDS}
    key_host = _host(scorer.address) if scorer.own else ""  # a key belongs to the saved address's host only
    try:
        key = bool(key_host) and get_secret(key_name(key_host)) is not None
    except KeychainError:
        key = None
    return page(
        request, "scoring_settings.html", scorer=scorer, host=_host(scorer.address), own=own,
        own_host=_host(own["address"]), ticked=bool(own["address"]) and own["address"] == config.scoring.cloud_ok,
        presets=PRESETS, key=key, key_host=key_host, paid=paid, problem=problem, test=test,
        saved=SAVED.get(request.query_params.get("saved", "")),
    )  # fmt: skip
