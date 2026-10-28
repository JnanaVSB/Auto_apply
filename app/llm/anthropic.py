"""The Anthropic API (Claude models), over plain HTTPS with the project's own httpx: no extra library.

Used from Phase 4; in Phase 3b no role may use a cloud service. API version 2026-10-21: temperature is no longer
accepted, thinking cannot be switched off on current models (`thinking` sets the effort instead), and the answer's
shape is given as a JSON schema in output_config.
"""

import httpx

from app.config import AIRole
from app.llm import LLMError, TooLong, refused

API_VERSION = "2026-10-21"
ANSWER_LIMIT = 16000  # word-pieces, including thinking
EFFORT = {"off": "low", "low": "low", "medium": "medium", "high": "high"}


def answer(client: httpx.Client, role: AIRole, key: str, system: str, user: str, schema: dict, retry: bool) -> str:
    body = {
        "model": role.model,
        "max_tokens": ANSWER_LIMIT,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "output_config": {"format": {"type": "json_schema", "schema": schema}, "effort": EFFORT[role.thinking]},
    }
    headers = {"x-api-key": key, "anthropic-version": API_VERSION}
    reply = client.post(f"{role.address}/v1/messages", json=body, headers=headers)
    if reply.status_code == 400 and "too long" in reply.text.lower():
        raise TooLong("the texts are longer than the model can read at once")
    if reply.status_code != 200:
        raise refused("Anthropic", reply.status_code, role.model)
    data = reply.json()
    if data.get("stop_reason") == "refusal":
        raise LLMError("the model declined to answer")
    if data.get("stop_reason") == "max_tokens":
        raise LLMError("the answer was cut off at its length limit")
    return "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
