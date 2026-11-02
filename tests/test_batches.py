"""Anthropic's half-price batches against a fake Anthropic (httpx's built-in fake web layer): nothing here reaches a
real AI."""

import json
from datetime import UTC, datetime

import httpx
import pytest

from app import llm
from app.config import AIRole
from app.keychain import SERVICE
from app.llm import anthropic

FAKE_KEY = "fake-key-123"  # tests only
CLAUDE = AIRole(service="anthropic", model="claude-sonnet-5-5", key_name="anthropic")
OPUS = AIRole(service="anthropic", model="claude-opus-5-5", key_name="anthropic", thinking="xhigh")
BATCHES = "https://api.anthropic.com/v1/messages/batches"
MESSAGE = {
    "content": [{"type": "text", "text": "{}"}], "stop_reason": "end_turn",
    "usage": {"input_tokens": 900, "cache_read_input_tokens": 50, "output_tokens": 400},
}  # fmt: skip


@pytest.fixture(autouse=True)
def key(fake_keychain):
    fake_keychain.store[(SERVICE, "anthropic")] = FAKE_KEY


def fake(*replies):
    requests, answers = [], iter(replies)

    def answer(request):
        requests.append(request)
        reply = next(answers)
        if isinstance(reply, Exception):
            raise reply
        return reply

    return httpx.MockTransport(answer), requests


def run(action, *args, replies=()):
    """One batch action through the usual connection; returns its result and the requests made."""
    transport, requests = fake(*replies)
    with llm.connected(CLAUDE, transport=transport) as (client, key):
        return action(client, CLAUDE, key, *args), requests


def test_a_batch_is_sent_with_each_request_named_and_its_id_comes_back():
    requests = [
        anthropic.BatchRequest("aa-7", CLAUDE, "rules", "texts", {"type": "object"}, 4096),
        anthropic.BatchRequest("aa-8", OPUS, "rules 2", "texts 2", {"type": "object"}),
    ]
    found, sent = run(anthropic.send_batch, requests, replies=[httpx.Response(200, json={"id": "msgbatch_01ab"})])
    assert found == "msgbatch_01ab"
    request, body = sent[0], json.loads(sent[0].content)
    assert (request.method, str(request.url)) == ("POST", BATCHES)
    assert (request.headers["x-api-key"], request.headers["anthropic-version"]) == (FAKE_KEY, "2026-10-21")
    assert [item["custom_id"] for item in body["requests"]] == ["aa-7", "aa-8"]
    first, second = (item["params"] for item in body["requests"])
    assert (first["model"], first["system"], first["max_tokens"], first["output_config"]["effort"]) == (
        "claude-sonnet-5-5", "rules", 4096 + 8000, "low",
    )  # fmt: skip
    assert (second["model"], second["max_tokens"], second["output_config"]["effort"]) == (
        "claude-opus-5-5", 8000 + 16000, "xhigh",
    )  # fmt: skip
    assert first["messages"] == [{"role": "user", "content": "texts"}] and "temperature" not in first


@pytest.mark.parametrize(
    ("reply", "error", "message"),
    [
        (httpx.Response(401, text=f"bad key {FAKE_KEY}"), llm.StopScoring, "refused the key"),
        (httpx.Response(529), llm.Busy, "busy or failing"),
        (httpx.Response(400), llm.LLMError, "refused the request"),  # nothing made
        (httpx.Response(200, json={"id": "../v1/files"}), llm.TimedOut, "could not be read"),  # it may exist
        (httpx.Response(200, json={"id": None}), llm.TimedOut, "could not be read"),
        (httpx.ConnectError("no route"), llm.StopScoring, "Could not reach api.anthropic.com"),  # never sent
    ],
)
def test_a_batch_that_could_not_be_sent_says_why_never_with_the_key(reply, error, message):
    request = anthropic.BatchRequest("aa-1", CLAUDE, "rules", "texts", {})
    with pytest.raises(error, match=message) as raised:
        run(anthropic.send_batch, [request], replies=[reply])
    assert type(raised.value) is error and FAKE_KEY not in str(raised.value)
    assert raised.value.reply is None or FAKE_KEY not in raised.value.reply


@pytest.mark.parametrize(("status", "ended"), [("in_progress", False), ("canceling", False), ("ended", True)])
def test_a_batch_says_whether_it_has_ended(status, ended):
    found, sent = run(anthropic.batch_ended, "msgbatch_01ab", replies=[httpx.Response(200, json={
        "processing_status": status,
    })])  # fmt: skip
    assert found is ended and str(sent[0].url) == f"{BATCHES}/msgbatch_01ab"


def test_a_batch_anthropic_no_longer_has_is_expired():
    for action in (anthropic.batch_ended, anthropic.batch_results):
        with pytest.raises(llm.Expired, match="no longer has this batch"):
            run(action, "msgbatch_01ab", replies=[httpx.Response(404)])


def test_an_odd_batch_id_never_goes_into_an_address():
    transport, requests = fake()
    for action in (anthropic.batch_ended, anthropic.batch_results, anthropic.delete_batch):
        with pytest.raises(llm.LLMError, match="did not answer as expected"):
            with llm.connected(CLAUDE, transport=transport) as (client, key):
                action(client, CLAUDE, key, "msgbatch/../../files")
    assert requests == []


def _line(name, result):
    return json.dumps({"custom_id": name, "result": result})


def test_each_result_is_the_answer_or_why_there_is_none():
    lines = [
        _line("aa-1", {"type": "succeeded", "message": MESSAGE}),
        _line("aa-2", {"type": "succeeded", "message": MESSAGE | {"stop_reason": "refusal"}}),
        _line("aa-3", {"type": "succeeded", "message": MESSAGE | {"stop_reason": "max_tokens"}}),
        _line("aa-4", {"type": "errored", "error": {"type": "error", "error": {
            "type": "invalid_request_error", "message": "prompt is too long: 1200000 tokens"}}}),
        _line("aa-5", {"type": "errored", "error": {"type": "error", "error": {"type": "invalid_request_error"}}}),
        _line("aa-6", {"type": "errored", "error": {"type": "error", "error": {"type": "overloaded_error"}}}),
        _line("aa-7", {"type": "errored", "error": {"type": "error", "error": {"type": "authentication_error"}}}),
        _line("aa-8", {"type": "expired"}),
        _line("aa-9", {"type": "canceled"}),
        _line("aa-10", {"type": "succeeded", "message": "garbled"}),
        "not json", json.dumps({"result": {"type": "succeeded"}}), json.dumps(["aa-11"]), "[" * 10_000,
    ]  # fmt: skip
    found, sent = run(anthropic.batch_results, "msgbatch_01ab", replies=[httpx.Response(200, text="\n".join(lines))])
    assert str(sent[0].url) == f"{BATCHES}/msgbatch_01ab/results"  # built here, never taken from Anthropic's answer
    assert found["aa-1"] == llm.Reply("{}", llm.Usage(900, 50, 400))
    expected = {
        "aa-2": (llm.LLMError, "declined"), "aa-3": (llm.LLMError, "cut off"), "aa-4": (llm.TooLong, "longer"),
        "aa-5": (llm.LLMError, "refused the request"), "aa-6": (llm.Busy, "not charged"),
        "aa-7": (llm.StopScoring, "refused the key"), "aa-8": (llm.Expired, "24 hours"),
        "aa-9": (llm.Expired, "24 hours"), "aa-10": (llm.LLMError, "did not answer as expected"),
    }  # fmt: skip
    assert set(found) == {"aa-1", *expected}  # broken lines are left out
    for name, (kind, words) in expected.items():
        assert type(found[name]) is kind and words in str(found[name])
    assert found["aa-2"].usage == found["aa-3"].usage == llm.Usage(900, 50, 400)  # charged all the same


@pytest.mark.parametrize("status", [200, 404])
def test_deleting_an_ended_batch_or_one_already_gone_is_fine(status):
    found, sent = run(anthropic.delete_batch, "msgbatch_01ab", replies=[httpx.Response(status)])
    assert found is None and (sent[0].method, str(sent[0].url)) == ("DELETE", f"{BATCHES}/msgbatch_01ab")
    with pytest.raises(llm.Busy):
        run(anthropic.delete_batch, "msgbatch_01ab", replies=[httpx.Response(500)])


def _page(*batches, more=False):
    listed = [{"id": name, "created_at": created} for name, created in batches]
    return httpx.Response(200, json={"data": listed, "has_more": more, "last_id": listed[-1]["id"] if listed else None})


def test_finding_the_batches_sent_since_a_time_reads_page_by_page_until_older_ones():
    since = datetime(2026, 11, 1, 22, 0, tzinfo=UTC)
    replies = [
        _page(("b3", "2026-11-01T23:10:00Z"), ("b2", "2026-11-01T22:00:00.5Z"), more=True),
        _page(("b1", "2026-11-01T21:59:59Z"), ("b0", "2026-10-31T10:00:00Z"), more=True),
    ]
    found, sent = run(anthropic.batches_since, since, replies=replies)
    assert found == ["b3", "b2"]
    assert [dict(request.url.params) for request in sent] == [{"limit": "100"}, {"limit": "100", "after_id": "b2"}]
    found, _ = run(anthropic.batches_since, since, replies=[_page(("b4", "2026-11-02T01:00:00Z"))])
    assert found == ["b4"]  # no more pages
    found, _ = run(anthropic.batches_since, since, replies=[_page(("b5", "yesterday"), ("b4", "2026-11-02T01:00Z"))])
    assert found == []  # a time it can't read: stops rather than guess
    found, _ = run(anthropic.batches_since, since, replies=[_page(("b6", "2026-11-02T01:00:00"))])
    assert found == []  # nor one without its time zone
