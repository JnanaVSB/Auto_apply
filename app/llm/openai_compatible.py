"""Any service that accepts OpenAI's chat-completions request (OpenAI itself, Groq, Ollama Cloud and others).

Used from Phase 4; in Phase 3b no role may use a cloud service.
"""

import httpx

from app.config import AIRole
from app.llm import LLMError, TooLong, refused

ANSWER_LIMIT = 4096  # word-pieces, including any thinking


def answer(client: httpx.Client, role: AIRole, key: str, system: str, user: str, schema: dict, retry: bool) -> str:
    body = {
        "model": role.model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema, "strict": True}},
        "max_completion_tokens": ANSWER_LIMIT,
    } | ({} if role.thinking == "off" else {"reasoning_effort": role.thinking})
    reply = client.post(f"{role.address}/chat/completions", json=body, headers={"Authorization": f"Bearer {key}"})
    if reply.status_code == 400 and "context" in reply.text.lower():
        raise TooLong("the texts are longer than the model can read at once")
    if reply.status_code != 200:
        raise refused("The AI service", reply.status_code, role.model)
    choice = (reply.json().get("choices") or [{}])[0]
    if choice.get("finish_reason") == "length":
        raise LLMError("the answer was cut off at its length limit")
    return choice.get("message", {}).get("content") or ""
