"""The AI connector against fake services (httpx's built-in fake web layer): nothing here reaches a real AI."""

import json

import httpx
import pytest

from app import llm
from app.config import AIRole
from app.keychain import SERVICE

FAKE_KEY = "fake-key-123"  # tests only
SHAPE = {"type": "object"}
LOCAL = AIRole(model="gemma4:26b-a4b-it-q8_0")
GROQ = AIRole(service="openai-compatible", model="openai/gpt-oss-120b", address="https://api.groq.com/openai/v1",
              key_name="groq")  # fmt: skip
CLAUDE = AIRole(service="anthropic", model="claude-sonnet-5-5", key_name="anthropic")


def fake(*replies):
    """A transport giving `replies` in turn (a response or an error to raise); returns it and the requests made."""
    requests, answers = [], iter(replies)

    def answer(request):
        requests.append(request)
        reply = next(answers)
        if isinstance(reply, Exception):
            raise reply
        return reply

    return httpx.MockTransport(answer), requests


def ollama_reply(content='{"score": 80, "reasons": []}', read=1000):
    return httpx.Response(200, json={"message": {"content": content}, "prompt_eval_count": read})


@pytest.fixture
def keys(fake_keychain):
    fake_keychain.store[(SERVICE, "groq")] = FAKE_KEY
    fake_keychain.store[(SERVICE, "anthropic")] = FAKE_KEY


def test_ollama_request_and_answer():
    transport, requests = fake(ollama_reply())
    assert llm.ask(LOCAL, "rules", "texts", SHAPE, transport=transport) == '{"score": 80, "reasons": []}'
    request = requests[0]
    body = json.loads(request.content)
    assert str(request.url) == "http://127.0.0.1:11434/api/chat"
    assert (body["model"], body["format"], body["think"], body["stream"]) == (
        "gemma4:26b-a4b-it-q8_0",
        SHAPE,
        False,
        False,
    )
    assert body["options"] == {"num_ctx": 16384, "temperature": 0, "seed": 1, "num_predict": 1024}
    assert [message["role"] for message in body["messages"]] == ["system", "user"]


def test_ollama_retry_is_a_little_less_strict():
    transport, requests = fake(ollama_reply())
    llm.ask(LOCAL, "rules", "texts", SHAPE, retry=True, transport=transport)
    assert json.loads(requests[0].content)["options"]["temperature"] == 0.3


@pytest.mark.parametrize(
    ("reply", "error", "message"),
    [
        (httpx.ConnectError("refused"), llm.StopScoring, "Ollama isn't running: start it with `ollama serve`"),
        (httpx.Response(404, json={"error": "model not found"}), llm.StopScoring, "run `ollama pull gemma4:26b"),
        (
            httpx.Response(500, json={"error": "CUDA error: out of memory"}),
            llm.StopScoring,
            "ran out of graphics memory",
        ),
        (httpx.Response(500, json={"error": "something else"}), llm.LLMError, "Ollama refused the request"),
        (httpx.ReadTimeout("slow"), llm.TimedOut, "no answer within 120 seconds"),
        (ollama_reply(read=10), llm.TooLong, "do not fit the model's working size of 16,384"),  # Ollama cut it
    ],
)
def test_ollama_problems_name_the_cause_and_the_fix(reply, error, message):
    transport, _ = fake(reply)
    with pytest.raises(error, match=message):
        llm.ask(LOCAL, "rules", "texts " * 20, SHAPE, transport=transport)


def test_openai_compatible_service(keys):
    reply = {"choices": [{"message": {"content": '{"score": 70}'}, "finish_reason": "stop"}]}
    transport, requests = fake(httpx.Response(200, json=reply))
    assert llm.ask(GROQ, "rules", "texts", SHAPE, transport=transport) == '{"score": 70}'
    request, body = requests[0], json.loads(requests[0].content)
    assert str(request.url) == "https://api.groq.com/openai/v1/chat/completions"
    assert request.headers["Authorization"] == f"Bearer {FAKE_KEY}"
    assert body["response_format"]["json_schema"] == {"name": "answer", "schema": SHAPE, "strict": True}
    assert "temperature" not in body and "reasoning_effort" not in body


def test_anthropic_service(keys):
    reply = {"content": [{"type": "text", "text": '{"score": 60}'}], "stop_reason": "end_turn"}
    transport, requests = fake(httpx.Response(200, json=reply))
    assert llm.ask(CLAUDE, "rules", "texts", SHAPE, transport=transport) == '{"score": 60}'
    request, body = requests[0], json.loads(requests[0].content)
    assert str(request.url) == "https://api.anthropic.com/v1/messages"
    assert (request.headers["x-api-key"], request.headers["anthropic-version"]) == (FAKE_KEY, "2026-10-21")
    assert body["output_config"] == {"format": {"type": "json_schema", "schema": SHAPE}, "effort": "low"}
    assert body["system"] == "rules" and "temperature" not in body


@pytest.mark.parametrize(
    ("role", "reply", "error", "message"),
    [
        (GROQ, httpx.Response(401), llm.StopScoring, "refused the key"),
        (CLAUDE, httpx.Response(404), llm.StopScoring, "does not know the model 'claude-sonnet-5-5'"),
        (CLAUDE, httpx.Response(529), llm.LLMError, "busy or failing"),
        (GROQ, httpx.Response(400, text="maximum context length exceeded"), llm.TooLong, "longer than the model"),
        (CLAUDE, httpx.Response(400, text="prompt is too long"), llm.TooLong, "longer than the model"),
        (CLAUDE, httpx.Response(200, json={"content": [], "stop_reason": "refusal"}), llm.LLMError, "declined"),
        (GROQ, httpx.ConnectError("no route"), llm.StopScoring, "Could not reach api.groq.com"),
    ],
)
def test_cloud_problems_never_show_the_key(keys, role, reply, error, message):
    transport, _ = fake(reply)
    with pytest.raises(error, match=message) as raised:
        llm.ask(role, "rules", "texts", SHAPE, transport=transport)
    assert FAKE_KEY not in str(raised.value)


def test_missing_key_is_named_and_nothing_is_sent():
    transport, requests = fake()
    with pytest.raises(llm.StopScoring, match="No key is saved in the keychain under the name 'groq'"):
        llm.ask(GROQ, "rules", "texts", SHAPE, transport=transport)
    assert requests == []


@pytest.mark.parametrize(
    ("reply", "ready", "line"),
    [
        (httpx.Response(200, json={"models": [{"name": "gemma4:26b-a4b-it-q8_0"}]}), True, "is downloaded"),
        (
            httpx.Response(200, json={"models": [{"name": "qwen3:14b"}]}),
            False,
            "run `ollama pull gemma4:26b-a4b-it-q8_0`",
        ),
        (httpx.ConnectError("refused"), False, "start it with `ollama serve`"),
    ],
)
def test_ollama_status_says_what_to_do(reply, ready, line):
    from app.llm import ollama

    transport, _ = fake(reply)
    found, text = ollama.status(LOCAL, transport)
    assert found == ready and line in text
