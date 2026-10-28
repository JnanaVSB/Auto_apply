"""Ollama on this computer: the model runs on your own graphics card, and nothing leaves the laptop."""

import httpx

from app.config import AIRole
from app.llm import LLMError, StopScoring, TooLong

KEEP_LOADED = "10m"  # the model stays in memory between jobs for this long
ANSWER_LIMIT = 1024  # word-pieces; with thinking on, the model needs more room
THINKING_ANSWER_LIMIT = 4096
CHARACTERS_PER_PIECE = 8  # any text has at least one word-piece per 8 characters; fewer read means a cut prompt
OUT_OF_MEMORY = ("out of memory", "cudamalloc", "cuda error")


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
