"""Any service that accepts OpenAI's chat-completions request (OpenAI itself, Groq, Ollama Cloud and others).

Used from Phase 4; in Phase 3b no role may use a cloud service.
"""

from urllib.parse import urlsplit

import httpx

from app.config import OPENAI_HOST, AIRole
from app.llm import LLMError, Reply, TooLong, Usage, answer_data, refused, tokens

ANSWER_LIMIT = 4096  # word-pieces, unless the request asks for more
THINKING_ROOM = 8000  # with thinking on, the model needs this much more room


def most_output(role: AIRole, limit: int | None) -> int:
    return (limit or ANSWER_LIMIT) + (0 if role.thinking == "off" else THINKING_ROOM)


def answer(
    client: httpx.Client, role: AIRole, key: str | None, system: str, user: str, schema: dict, retry: bool,
    limit: int | None,
) -> Reply:  # fmt: skip
    body = {
        "model": role.model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema, "strict": True}},
        "max_completion_tokens": most_output(role, limit),
    } | ({} if role.thinking == "off" else {"reasoning_effort": role.thinking})
    if urlsplit(role.address).hostname == OPENAI_HOST:
        body["store"] = False  # OpenAI keeps nothing (owner decision D6); other hosts may refuse the setting
    headers = {"Authorization": f"Bearer {key}"} if key else {}  # your own server may need no key
    reply = client.post(f"{role.address}/chat/completions", json=body, headers=headers)
    if reply.status_code == 400 and "context" in reply.text.lower():
        raise TooLong("the texts are longer than the model can read at once")
    if reply.status_code != 200:
        raise refused("The AI service", reply.status_code, role.model)
    data = answer_data(reply)
    cached = tokens(data, "usage", "prompt_tokens_details", "cached_tokens")  # counted in prompt_tokens too
    usage = Usage(
        max(tokens(data, "usage", "prompt_tokens") - cached, 0), cached, tokens(data, "usage", "completion_tokens")
    )
    choices = data.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    if choice.get("finish_reason") == "length":
        raise LLMError("the answer was cut off at its length limit", usage)
    message = choice.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return Reply(content if isinstance(content, str) else "", usage)  # a refusal has no content: a bad answer
