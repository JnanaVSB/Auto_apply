"""The Anthropic API (Claude models), over plain HTTPS with the project's own httpx: no extra library.

Used from Phase 4; in Phase 3b no role may use a cloud service. API version 2026-10-21: temperature is no longer
accepted, thinking cannot be switched off on current models (`thinking` sets the effort instead), and the answer's
shape is given as a JSON schema in output_config.

Half price is Anthropic's Message Batches: many requests sent at once, each answered within 24 hours, collected
later. The app builds every batch address itself from the role's own address, so your key only goes to Anthropic.
"""

import json
import re
from datetime import datetime
from typing import NamedTuple

import httpx

from app.config import AIRole
from app.llm import Busy, Expired, LLMError, Reply, StopScoring, TimedOut, TooLong, Usage, answer_data, refused, tokens

API_VERSION = "2026-10-21"
ANSWER_LIMIT = 8000  # word-pieces, unless the request asks for more
THINKING_ROOM, XHIGH_ROOM = 8000, 16000  # these models always think; at xhigh, longer
EFFORT = {"off": "low", "low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh"}
BATCH_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")  # an id Anthropic gave; anything else never goes into an address
PAGES = 20  # the most pages of your batches read when looking for one a crash lost
GONE = "Anthropic no longer has this batch"


class BatchRequest(NamedTuple):
    name: str  # matches the answer to its request: aa-<step id>
    role: AIRole
    system: str
    user: str
    schema: dict
    limit: int | None = None


def most_output(role: AIRole, limit: int | None) -> int:
    return (limit or ANSWER_LIMIT) + (XHIGH_ROOM if role.thinking == "xhigh" else THINKING_ROOM)


def check(client: httpx.Client, role: AIRole, key: str) -> tuple[bool, str]:
    reply = client.get(f"{role.address}/v1/models/{role.model}", headers=_headers(key))
    if reply.status_code != 200:
        return False, str(refused("Anthropic", reply.status_code, role.model))
    return True, f"Anthropic accepted the key and knows the model {role.model}."


def answer(
    client: httpx.Client, role: AIRole, key: str, system: str, user: str, schema: dict, retry: bool,
    limit: int | None, half: bool,
) -> Reply:  # fmt: skip
    if half:
        raise ValueError("Anthropic's half price is only through its batches")  # so a cost is never counted wrong
    reply = client.post(
        f"{role.address}/v1/messages", json=_body(role, system, user, schema, limit), headers=_headers(key)
    )
    if reply.status_code == 400 and "too long" in reply.text.lower():
        raise TooLong("the texts are longer than the model can read at once")
    if reply.status_code != 200:
        raise refused("Anthropic", reply.status_code, role.model)
    return _reply(answer_data(reply))


def send_batch(client: httpx.Client, role: AIRole, key: str, requests: list[BatchRequest]) -> str:
    """Sends the requests as one batch at half price; returns the batch's id, to be saved at once."""
    body = {
        "requests": [
            {"custom_id": request.name, "params": _body(request.role, request.system, request.user, request.schema,
                                                        request.limit)}
            for request in requests
        ]
    }  # fmt: skip
    reply = client.post(f"{role.address}/v1/messages/batches", json=body, headers=_headers(key))
    if reply.status_code != 200:
        raise refused("Anthropic", reply.status_code, role.model)
    try:
        return _batch_id(answer_data(reply).get("id"))
    except LLMError:  # accepted, but its id can't be read: the batch may exist, like a lost answer
        raise TimedOut("Anthropic's answer to the batch could not be read") from None


def batch_ended(client: httpx.Client, role: AIRole, key: str, batch_id: str) -> bool:
    """Whether every request of the batch has its result."""
    reply = client.get(f"{role.address}/v1/messages/batches/{_batch_id(batch_id)}", headers=_headers(key))
    if reply.status_code == 404:
        raise Expired(GONE)
    if reply.status_code != 200:
        raise refused("Anthropic", reply.status_code, role.model)
    return answer_data(reply).get("processing_status") == "ended"


def batch_results(client: httpx.Client, role: AIRole, key: str, batch_id: str) -> dict[str, Reply | LLMError]:
    """An ended batch's result for each request, by its name: the answer, or why there is none (an error)."""
    reply = client.get(f"{role.address}/v1/messages/batches/{_batch_id(batch_id)}/results", headers=_headers(key))
    if reply.status_code == 404:
        raise Expired(GONE)
    if reply.status_code != 200:
        raise refused("Anthropic", reply.status_code, role.model)
    found: dict[str, Reply | LLMError] = {}
    for line in reply.text.splitlines():
        try:
            item = json.loads(line)
        except (ValueError, RecursionError):
            continue
        name = item.get("custom_id") if isinstance(item, dict) else None
        result = item.get("result") if isinstance(name, str) else None
        if isinstance(result, dict):
            found[name] = _result(result)
    return found


def delete_batch(client: httpx.Client, role: AIRole, key: str, batch_id: str) -> None:
    """Deletes an ended batch at Anthropic, with its texts and results; one already gone is fine."""
    reply = client.delete(f"{role.address}/v1/messages/batches/{_batch_id(batch_id)}", headers=_headers(key))
    if reply.status_code not in (200, 404):
        raise refused("Anthropic", reply.status_code, role.model)


def batches_since(client: httpx.Client, role: AIRole, key: str, since: datetime) -> list[str]:
    """The ids of your batches created at or after `since` (a time with its time zone), newest first."""
    found, after = [], None
    for _ in range(PAGES):
        params = {"limit": 100} | ({"after_id": after} if after else {})
        reply = client.get(f"{role.address}/v1/messages/batches", params=params, headers=_headers(key))
        if reply.status_code != 200:
            raise refused("Anthropic", reply.status_code, role.model)
        data = answer_data(reply)
        listed = data.get("data") if isinstance(data.get("data"), list) else []
        for batch in listed:
            created = _time(batch.get("created_at") if isinstance(batch, dict) else None)
            if created is None or created < since:
                return found  # newest first: everything after this one is older
            found.append(_batch_id(batch.get("id")))
        if data.get("has_more") is not True or not listed:
            return found
        after = _batch_id(data.get("last_id"))
    return found


def _headers(key: str) -> dict[str, str]:
    return {"x-api-key": key, "anthropic-version": API_VERSION}


def _body(role: AIRole, system: str, user: str, schema: dict, limit: int | None) -> dict:
    return {
        "model": role.model,
        "max_tokens": most_output(role, limit),
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "output_config": {"format": {"type": "json_schema", "schema": schema}, "effort": EFFORT[role.thinking]},
    }


def _reply(data: dict) -> Reply:
    """A message's answer and its tokens; a declined or cut-off answer is one failed try that was charged."""
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


def _result(result: dict) -> Reply | LLMError:
    """One request's result in a batch. Only a succeeded one is charged; the others are sent again or refused."""
    kind = result.get("type")
    if kind == "succeeded":
        message = result.get("message")
        if not isinstance(message, dict):
            return LLMError("the service did not answer as expected")
        try:
            return _reply(message)
        except LLMError as error:
            return error
    if kind != "errored":  # expired, canceled (the app never cancels), or something new: never charged
        return Expired("Anthropic didn't get to it within 24 hours: it is sent again with the next batch")
    error = result.get("error") if isinstance(result.get("error"), dict) else {}
    error = error.get("error") if isinstance(error.get("error"), dict) else error  # {"type": "error", "error": {...}}
    kind, said = str(error.get("type", "")), str(error.get("message", "")).lower()
    if kind.startswith("invalid_request"):
        if "too long" in said:
            return TooLong("the texts are longer than the model can read at once")
        return LLMError("Anthropic refused the request")
    if kind in ("authentication_error", "permission_error"):
        return StopScoring("Anthropic refused the key: check the key saved in the keychain.")
    return Busy("Anthropic had a problem with this request (not charged); it is sent again later.")


def _batch_id(value: object) -> str:
    if not isinstance(value, str) or not BATCH_ID.fullmatch(value):
        raise LLMError("the service did not answer as expected")
    return value


def _time(value: object) -> datetime | None:
    try:
        found = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None
    return found if found and found.tzinfo else None
