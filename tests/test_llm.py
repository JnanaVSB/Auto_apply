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


def ollama_reply(content='{"score": 80, "reasons": []}'):
    return httpx.Response(200, json={"message": {"content": content}, "prompt_eval_count": 1000})


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
    assert body["truncate"] is False  # Ollama refuses a too-long prompt instead of cutting it
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
        (
            httpx.Response(500, json={"error": "model requires more system memory (15.2 GiB) than is available"}),
            llm.StopScoring,
            r"Ollama refused the request \(status 500: .*more system memory .*\)\. Restart it",
        ),
        (httpx.Response(500, text="llama runner process has terminated"), llm.StopScoring, "runner process"),
        (httpx.Response(400, json={"error": "think value not supported"}), llm.StopScoring, "status 400"),
        (
            httpx.Response(400, json={"error": "request (10013 tokens) exceeds the available context size"}),
            llm.TooLong,
            "do not fit the model's working size of 16,384",
        ),
        (httpx.Response(200, text="<html>not Ollama</html>"), llm.StopScoring, "did not answer as expected"),
        (httpx.Response(200, json=["not", "an", "answer"]), llm.StopScoring, "did not answer as expected"),
        (httpx.Response(200, json={"done": True}), llm.StopScoring, "did not answer as expected"),
        (httpx.ReadTimeout("slow"), llm.TimedOut, "no answer within 120 seconds"),
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


DOWNLOADED = httpx.Response(200, json={"models": [{"name": "gemma4:26b-a4b-it-q8_0"}]})


def version(number):
    return httpx.Response(200, json={"version": number})


@pytest.mark.parametrize(
    ("replies", "ready", "line"),
    [
        ((DOWNLOADED, version("0.32.15")), True, "is downloaded"),
        ((DOWNLOADED, version("1.0.2-rc1")), True, "is downloaded"),
        ((httpx.Response(200, json={"models": [{"name": "qwen3:14b"}]}), version("0.32.15")), False,
         "run `ollama pull gemma4:26b-a4b-it-q8_0`"),
        ((httpx.ConnectError("refused"),), False, "start it with `ollama serve`"),
        ((httpx.Response(200, text="not JSON"),), False, "did not answer as expected"),
        ((httpx.Response(200, json={"models": ["no names"]}),), False, "did not answer as expected"),
        ((DOWNLOADED, version("0.31.2")), False, "Ollama 0.31.2 is older than 0.32.15: update it"),
        ((DOWNLOADED, version("0.32.9")), False, "Ollama 0.32.9 is older than 0.32.15"),
        # a version that cannot be read, or 0.0.0 (built from source code), is unknown: nothing is blocked
        ((DOWNLOADED, httpx.Response(404)), True, "is downloaded"),
        ((DOWNLOADED, version(32)), True, "is downloaded"),
        ((DOWNLOADED, version("dev")), True, "is downloaded"),
        ((DOWNLOADED, version("0.0.0")), True, "is downloaded"),
        ((DOWNLOADED, httpx.ReadTimeout("slow")), True, "is downloaded"),
    ],
)  # fmt: skip
def test_ollama_status_says_what_to_do(replies, ready, line):
    from app.llm import ollama

    transport, requests = fake(*replies)
    found, text = ollama.status(LOCAL, transport)
    assert found == ready and line in text
    paths = ["/api/tags", "/api/version"][: len(replies)]
    assert [str(request.url) for request in requests] == [f"http://127.0.0.1:11434{path}" for path in paths]


@pytest.mark.parametrize(
    ("reply", "names"),
    [
        (httpx.Response(200, json={"models": [{"name": "qwen3.6:35b-a3b"}, {"name": "gemma4:26b-a4b-it-qat"}]}),
         ["gemma4:26b-a4b-it-qat", "qwen3.6:35b-a3b"]),
        (httpx.Response(500), []),
        (httpx.Response(200, text="not JSON"), []),
        (httpx.ConnectError("refused"), []),
    ],
)  # fmt: skip
def test_ollama_lists_its_downloaded_models(reply, names):
    from app.llm import ollama

    transport, requests = fake(reply)
    assert ollama.installed(LOCAL, transport) == names
    assert str(requests[0].url) == "http://127.0.0.1:11434/api/tags"  # only Ollama on this computer is asked


OPENAI = AIRole(service="openai-compatible", model="gpt-6.1-sol", address="https://api.openai.com/v1", key_name="groq")
OWN = AIRole(service="openai-compatible", model="my-model", address="http://192.168.1.5:8000/v1")  # no key
OPENAI_ANSWER = {
    "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 1200, "prompt_tokens_details": {"cached_tokens": 200}, "completion_tokens": 300},
}
ANTHROPIC_ANSWER = {
    "content": [{"type": "thinking", "thinking": "hmm", "text": "not the answer"}, {"type": "text", "text": "{}"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 900, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 50,
              "output_tokens": 400},
}  # fmt: skip


@pytest.mark.parametrize(
    ("role", "reply", "usage"),
    [
        (LOCAL, {"message": {"content": "{}"}, "prompt_eval_count": 1000, "eval_count": 250}, (1000, 0, 250)),
        (OPENAI, OPENAI_ANSWER, (1000, 200, 300)),  # cached tokens are part of prompt_tokens
        (CLAUDE, ANTHROPIC_ANSWER, (1000, 50, 400)),  # only the text block is the answer
        (CLAUDE, {"content": [{"type": "text", "text": "{}"}], "usage": {"input_tokens": "lots"}}, (0, 0, 0)),
    ],
)
def test_every_answer_comes_with_its_tokens(keys, role, reply, usage):
    transport, _ = fake(httpx.Response(200, json=reply))
    assert llm.call(role, "rules", "texts", SHAPE, transport=transport) == llm.Reply("{}", llm.Usage(*usage))


@pytest.mark.parametrize(
    ("role", "reply", "usage"),
    [
        (LOCAL, {"message": {"content": '{"sc'}, "done_reason": "length", "prompt_eval_count": 9, "eval_count": 7},
         (9, 0, 7)),
        (OPENAI, OPENAI_ANSWER | {"choices": [{"message": {"content": "{"}, "finish_reason": "length"}]},
         (1000, 200, 300)),
        (CLAUDE, ANTHROPIC_ANSWER | {"stop_reason": "max_tokens"}, (1000, 50, 400)),
        (CLAUDE, ANTHROPIC_ANSWER | {"stop_reason": "refusal"}, (1000, 50, 400)),
    ],
)  # fmt: skip
def test_a_failed_answer_still_says_what_was_charged(keys, role, reply, usage):
    transport, _ = fake(httpx.Response(200, json=reply))
    with pytest.raises(llm.LLMError, match="cut off|declined") as raised:
        llm.call(role, "rules", "texts", SHAPE, transport=transport)
    assert raised.value.usage == llm.Usage(*usage)


@pytest.mark.parametrize("reply", [httpx.Response(200, text="<html>"), httpx.Response(200, json=["a"])])
@pytest.mark.parametrize("role", [OPENAI, CLAUDE])
def test_a_cloud_answer_that_is_not_json_is_one_failed_try(keys, role, reply):
    transport, _ = fake(reply)
    with pytest.raises(llm.LLMError, match="did not answer as expected") as raised:
        llm.call(role, "rules", "texts", SHAPE, transport=transport)
    assert type(raised.value) is llm.LLMError and raised.value.usage == llm.Usage()


@pytest.mark.parametrize(
    ("role", "reply", "field", "usual", "asked"),
    [
        (LOCAL, ollama_reply(), ("options", "num_predict"), 1024, 8192),
        (LOCAL.model_copy(update={"thinking": "low"}), ollama_reply(), ("options", "num_predict"), 4096, 11264),
        (OPENAI, httpx.Response(200, json=OPENAI_ANSWER), ("max_completion_tokens",), 4096, 8192),
        (OPENAI.model_copy(update={"thinking": "high"}), httpx.Response(200, json=OPENAI_ANSWER),
         ("max_completion_tokens",), 12096, 16192),
        (CLAUDE, httpx.Response(200, json=ANTHROPIC_ANSWER), ("max_tokens",), 16000, 16192),
    ],
)  # fmt: skip
def test_a_request_can_ask_for_more_answer_room(keys, role, reply, field, usual, asked):
    transport, requests = fake(reply, reply)
    llm.call(role, "rules", "texts", SHAPE, transport=transport)
    llm.call(role, "rules", "texts", SHAPE, answer_limit=8192, transport=transport)
    assert [_at(json.loads(request.content), field) for request in requests] == [usual, asked]
    assert (llm.most_output(role), llm.most_output(role, 8192)) == (usual, asked)


def _at(body, field):
    for name in field:
        body = body[name]
    return body


def test_a_request_can_wait_longer():
    transport, requests = fake(ollama_reply(), httpx.ReadTimeout("slow"))
    llm.call(LOCAL, "rules", "texts", SHAPE, timeout=600, transport=transport)
    assert requests[0].extensions["timeout"]["read"] == 600
    with pytest.raises(llm.TimedOut, match="no answer within 600 seconds"):
        llm.call(LOCAL, "rules", "texts", SHAPE, timeout=600, transport=transport)


def test_only_openai_is_told_to_keep_nothing(keys):
    transport, requests = fake(*[httpx.Response(200, json=OPENAI_ANSWER)] * 3)
    for role in (OPENAI, GROQ, OWN):
        llm.call(role, "rules", "texts", SHAPE, transport=transport)
    assert [json.loads(request.content).get("store") for request in requests] == [False, None, None]


def test_your_own_address_without_a_key_is_sent_no_key(fake_keychain):
    transport, requests = fake(httpx.Response(200, json=OPENAI_ANSWER))
    assert llm.call(OWN, "rules", "texts", SHAPE, transport=transport).text == "{}"
    assert str(requests[0].url) == "http://192.168.1.5:8000/v1/chat/completions"
    assert "Authorization" not in requests[0].headers
