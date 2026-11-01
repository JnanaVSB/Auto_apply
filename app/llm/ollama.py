"""Ollama on this computer: the model runs on your own graphics card, and nothing leaves the laptop."""

import re

import httpx

from app.config import AIRole
from app.llm import LLMError, Reply, StopScoring, TooLong, Usage, tokens

KEEP_LOADED = "10m"  # the model stays in memory between jobs for this long
ANSWER_LIMIT = 1024  # word-pieces, unless the request asks for more
THINKING_ROOM = 3072  # with thinking on, the model needs this much more room
OUT_OF_MEMORY = ("out of memory", "cudamalloc", "cuda error")
TOO_LONG = ("exceed_context_size", "exceeds the available context size")  # its refusal while told not to cut
DETAIL = 200  # characters of Ollama's own error text shown; it names the cause and never holds the texts sent
STATUS_SECONDS = 5
UNEXPECTED = "Ollama did not answer as expected: restart it with `ollama serve`."
MIN_VERSION = (0, 32, 15)  # tested to refuse a too-long prompt when told not to cut it; an older one may cut it
VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def most_output(role: AIRole, limit: int | None) -> int:
    return (limit or ANSWER_LIMIT) + (0 if role.thinking == "off" else THINKING_ROOM)


def answer(
    client: httpx.Client, role: AIRole, key: None, system: str, user: str, schema: dict, retry: bool, limit: int | None
) -> Reply:
    body = {
        "model": role.model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "format": schema,
        "think": False if role.thinking == "off" else role.thinking,
        "stream": False,
        "keep_alive": KEEP_LOADED,
        "truncate": False,  # refuse a too-long prompt instead of silently cutting its start, where the resume is
        "options": {
            "num_ctx": role.context,
            "temperature": 0.3 if retry else 0,
            "seed": 2 if retry else 1,
            "num_predict": most_output(role, limit),
        },
    }
    reply = client.post(f"{role.address}/api/chat", json=body)
    if reply.status_code == 404:
        raise StopScoring(f"The model {role.model} is not downloaded: run `ollama pull {role.model}`.")
    if reply.status_code != 200:
        text = reply.text.lower()
        if reply.status_code == 400 and any(sign in text for sign in TOO_LONG):  # only this job's texts are at fault
            raise TooLong(
                f"the texts (about {(len(system) + len(user)) // 4:,} word-pieces) do not fit the model's working "
                f"size of {role.context:,}"
            )
        if any(sign in text for sign in OUT_OF_MEMORY):
            raise StopScoring(
                "The AI model ran out of graphics memory: close programs using the graphics card, or choose a "
                "smaller model."
            )
        detail = " ".join(reply.text.split())[:DETAIL]
        raise StopScoring(  # any other refusal would refuse every job alike: memory, a crashed runner, a setting
            f"Ollama refused the request (status {reply.status_code}: {detail}). Restart it with `ollama serve`, or "
            "check the AI model settings."
        )
    try:
        data = reply.json()
        content = data["message"]["content"]
    except (ValueError, RecursionError, TypeError, KeyError):
        content = None
    if not isinstance(content, str):
        raise StopScoring(UNEXPECTED)
    usage = Usage(tokens(data, "prompt_eval_count"), 0, tokens(data, "eval_count"))
    if data.get("done_reason") == "length":
        raise LLMError("the answer was cut off at its length limit", usage)
    return Reply(content, usage)


def _get(role: AIRole, path: str, transport: httpx.BaseTransport | None) -> httpx.Response:
    """One of Ollama's own pages: its list of downloaded models, or its version."""
    with httpx.Client(transport=transport, timeout=STATUS_SECONDS, follow_redirects=False, trust_env=False) as client:
        return client.get(f"{role.address}{path}")


def _names(reply: httpx.Response) -> set[str] | None:
    """The model names in Ollama's list; None when the reply is not such a list."""
    try:
        return {name for model in reply.json()["models"] if isinstance(name := model.get("name"), str)}
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def _version(role: AIRole, transport: httpx.BaseTransport | None) -> tuple[int, ...] | None:
    """Ollama's version number; None when it cannot be read."""
    try:
        found = VERSION.match(_get(role, "/api/version", transport).json()["version"])
    except (httpx.TransportError, ValueError, TypeError, KeyError):
        return None
    return tuple(int(number) for number in found.groups()) if found else None


def status(role: AIRole, transport: httpx.BaseTransport | None = None) -> tuple[bool, str]:
    """Whether Ollama is running with the role's model downloaded, and one line saying so or giving the fix."""
    try:
        reply = _get(role, "/api/tags", transport)
    except httpx.TransportError:
        return False, "Ollama isn't running: start it with `ollama serve` (install it first if needed)."
    if reply.status_code != 200:
        return False, f"Ollama did not answer as expected (status {reply.status_code}): restart it with `ollama serve`."
    names = _names(reply)
    if names is None:
        return False, UNEXPECTED
    version = _version(role, transport)
    if version and (0, 0, 0) < version < MIN_VERSION:  # unknown, or 0.0.0 (built from source code): not blocked
        older, needed = (".".join(map(str, numbers)) for numbers in (version, MIN_VERSION))
        return False, f"Ollama {older} is older than {needed}: update it, so that long ads are never cut."
    if role.model in names or f"{role.model}:latest" in names:
        return True, f"Ollama is running and the model {role.model} is downloaded."
    return False, f"Ollama is running, but the model {role.model} is not downloaded: run `ollama pull {role.model}`."


def installed(role: AIRole, transport: httpx.BaseTransport | None = None) -> list[str]:
    """The models downloaded in Ollama, by name; none when it does not answer as expected."""
    try:
        reply = _get(role, "/api/tags", transport)
    except httpx.TransportError:
        return []
    return sorted(_names(reply) or ()) if reply.status_code == 200 else []
