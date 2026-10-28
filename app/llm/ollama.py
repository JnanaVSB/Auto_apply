"""Ollama on this computer: the model runs on your own graphics card, and nothing leaves the laptop."""

import httpx

from app.config import AIRole
from app.llm import LLMError, StopScoring, TooLong

KEEP_LOADED = "10m"  # the model stays in memory between jobs for this long
ANSWER_LIMIT = 1024  # word-pieces; with thinking on, the model needs more room
THINKING_ANSWER_LIMIT = 4096
CHARACTERS_PER_PIECE = 8  # any text has at least one word-piece per 8 characters; fewer read means a cut prompt
OUT_OF_MEMORY = ("out of memory", "cudamalloc", "cuda error")
STATUS_SECONDS = 5


def answer(client: httpx.Client, role: AIRole, key: None, system: str, user: str, schema: dict, retry: bool) -> str:
    body = {
        "model": role.model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "format": schema,
        "think": False if role.thinking == "off" else role.thinking,
        "stream": False,
        "keep_alive": KEEP_LOADED,
        "options": {
            "num_ctx": role.context,
            "temperature": 0.3 if retry else 0,
            "seed": 2 if retry else 1,
            "num_predict": ANSWER_LIMIT if role.thinking == "off" else THINKING_ANSWER_LIMIT,
        },
    }
    reply = client.post(f"{role.address}/api/chat", json=body)
    if reply.status_code == 404:
        raise StopScoring(f"The model {role.model} is not downloaded: run `ollama pull {role.model}`.")
    if reply.status_code != 200:
        if any(sign in reply.text.lower() for sign in OUT_OF_MEMORY):
            raise StopScoring(
                "The AI model ran out of graphics memory: close programs using the graphics card, or choose a "
                "smaller model."
            )
        raise LLMError(f"Ollama refused the request (status {reply.status_code}).")
    data = reply.json()
    sent = len(system) + len(user)
    if data.get("prompt_eval_count", 0) * CHARACTERS_PER_PIECE < sent:  # Ollama cuts long prompts without saying so
        raise TooLong(
            f"the texts (about {sent // 4:,} word-pieces) do not fit the model's working size of {role.context:,}"
        )
    return data.get("message", {}).get("content", "")


def _tags(role: AIRole, transport: httpx.BaseTransport | None) -> httpx.Response:
    """Ollama's list of downloaded models."""
    with httpx.Client(transport=transport, timeout=STATUS_SECONDS, follow_redirects=False, trust_env=False) as client:
        return client.get(f"{role.address}/api/tags")


def status(role: AIRole, transport: httpx.BaseTransport | None = None) -> tuple[bool, str]:
    """Whether Ollama is running with the role's model downloaded, and one line saying so or giving the fix."""
    try:
        reply = _tags(role, transport)
    except httpx.TransportError:
        return False, "Ollama isn't running: start it with `ollama serve` (install it first if needed)."
    if reply.status_code != 200:
        return False, f"Ollama did not answer as expected (status {reply.status_code}): restart it with `ollama serve`."
    names = {model.get("name") for model in reply.json().get("models", [])}
    if role.model in names or f"{role.model}:latest" in names:
        return True, f"Ollama is running and the model {role.model} is downloaded."
    return False, f"Ollama is running, but the model {role.model} is not downloaded: run `ollama pull {role.model}`."


def installed(role: AIRole, transport: httpx.BaseTransport | None = None) -> list[str]:
    """The models downloaded in Ollama, by name; none when it does not answer."""
    try:
        reply = _tags(role, transport)
    except httpx.TransportError:
        return []
    if reply.status_code != 200:
        return []
    return sorted({name for model in reply.json().get("models", []) if isinstance(name := model.get("name"), str)})
