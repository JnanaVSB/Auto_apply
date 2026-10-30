"""The Anthropic API (Claude models), over plain HTTPS with the project's own httpx: no extra library.

Used from Phase 4; in Phase 3b no role may use a cloud service. API version 2026-10-21: temperature is no longer
accepted, thinking cannot be switched off on current models (`thinking` sets the effort instead), and the answer's
shape is given as a JSON schema in output_config.
"""

import httpx

from app.config import AIRole
from app.llm import LLMError, Reply, TooLong, Usage, answer_data, refused, tokens

API_VERSION = "2026-10-21"
ANSWER_LIMIT = 8000  # word-pieces, unless the request asks for more
THINKING_ROOM = 8000  # these models always think
EFFORT = {"off": "low", "low": "low", "medium": "medium", "high": "high"}


def most_output(role: AIRole, limit: int | None) -> int:
    return (limit or ANSWER_LIMIT) + THINKING_ROOM


def check(client: httpx.Client, role: AIRole, key: str) -> tuple[bool, str]:
    reply = client.get(
        f"{role.address}/v1/models/{role.model}", headers={"x-api-key": key, "anthropic-version": API_VERSION}
    )
    if reply.status_code != 200:
        return False, str(refused("Anthropic", reply.status_code, role.model))
    return True, f"Anthropic accepted the key and knows the model {role.model}."


def answer(
    client: httpx.Client, role: AIRole, key: str, system: str, user: str, schema: dict, retry: bool, limit: int | None
) -> Reply:
    body = {
        "model": role.model,
        "max_tokens": most_output(role, limit),
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
    data = answer_data(reply)
    usage = Usage(  # the app never asks to store texts in the cache, so cache writes are rare: counted as input
        tokens(data, "usage", "input_tokens") + tokens(data, "usage", "cache_creation_input_tokens"),
        tokens(data, "usage", "cache_read_input_tokens"),
        tokens(data, "usage", "output_tokens"),
    )
    if data.get("stop_reason") == "refusal":
        raise LLMError("the model declined to answer", usage)
    if data.get("stop_reason") == "max_tokens":
        raise LLMError("the answer was cut off at its length limit", usage)
    blocks = data["content"] if isinstance(data.get("content"), list) else []
    texts = [block.get("text") for block in blocks if isinstance(block, dict) and block.get("type") == "text"]
    return Reply("".join(text for text in texts if isinstance(text, str)), usage)
