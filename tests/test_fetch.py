"""Downloads, tested against httpx's built-in fake web layer; nothing here reaches the internet."""

import httpx
import pytest

from app import fetch

LEVER = "https://api.lever.co/v0/postings/acme?mode=json"
GREENHOUSE = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"


class FakeClock:
    """Stands in for the time module: records waits instead of sleeping."""

    def __init__(self) -> None:
        self.now = 0.0
        self.waits: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds


@pytest.fixture(autouse=True)
def clock(monkeypatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(fetch, "time", fake)
    return fake


def fake_web(*replies):
    """A Fetcher whose web layer gives `replies` in turn (a response or an error); returns it and the requests."""
    requests = []
    answers = iter(replies)

    def answer(request):
        requests.append(request)
        reply = next(answers)
        if isinstance(reply, Exception):
            raise reply
        return reply

    return fetch.Fetcher(httpx.MockTransport(answer)), requests


def test_returns_data_and_version_tag_and_says_who_it_is():
    fetcher, requests = fake_web(httpx.Response(200, json=[{"id": 1}], headers={"ETag": '"v1"'}))
    assert fetcher.get_json(LEVER) == ([{"id": 1}], '"v1"')
    assert requests[0].headers["User-Agent"] == fetch.USER_AGENT


@pytest.mark.parametrize(
    "url",
    [
        "http://api.lever.co/v0/postings/acme",
        "https://simplify.jobs/p/123",
        "https://api.lever.co.evil.example/x",
        "https://someone@api.lever.co/x",
        "https://[not a url",
    ],
)
def test_only_https_to_the_allowed_hosts(url):
    fetcher, requests = fake_web()
    with pytest.raises(fetch.FetchError, match="address not allowed"):
        fetcher.get_json(url)
    assert requests == []


def test_redirect_is_reported_not_followed():
    fetcher, requests = fake_web(httpx.Response(301, headers={"Location": "https://elsewhere.example/"}))
    with pytest.raises(fetch.FetchError, match="moved to another address"):
        fetcher.get_json(LEVER)
    assert len(requests) == 1


def test_not_found_is_not_retried():
    fetcher, requests = fake_web(httpx.Response(404))
    with pytest.raises(fetch.NotFound, match=r"not found \(404\)"):
        fetcher.get_json(LEVER)
    assert len(requests) == 1


def test_busy_site_is_retried_with_growing_waits(clock):
    fetcher, _ = fake_web(httpx.Response(429), httpx.Response(503), httpx.Response(200, json=[]))
    assert fetcher.get_json(LEVER) == ([], None)
    assert clock.waits == [2, 4]


def test_timeouts_and_dropped_connections_are_retried():
    fetcher, _ = fake_web(httpx.ReadTimeout("slow"), httpx.ConnectError("dropped"), httpx.Response(200, json={}))
    assert fetcher.get_json(LEVER) == ({}, None)


def test_repeated_timeouts_are_reported_as_timed_out():
    fetcher, _ = fake_web(*[httpx.ConnectTimeout("slow")] * 4)
    with pytest.raises(fetch.FetchError, match="timed out; gave up after 3 retries"):
        fetcher.get_json(LEVER)


def test_broken_compression_is_retried():
    fetcher, _ = fake_web(httpx.DecodingError("bad gzip"), httpx.Response(200, json=[]))
    assert fetcher.get_json(LEVER) == ([], None)


def test_absurdly_nested_json_is_refused():
    fetcher, _ = fake_web(httpx.Response(200, content=b"[" * 100_000 + b"]" * 100_000))
    with pytest.raises(fetch.FetchError, match="not JSON"):
        fetcher.get_json(LEVER)


def test_gives_up_after_three_retries_with_the_reason():
    fetcher, requests = fake_web(*[httpx.Response(500)] * 4)
    with pytest.raises(fetch.FetchError, match=r"status 500\); gave up after 3 retries"):
        fetcher.get_json(LEVER)
    assert len(requests) == 4


def test_site_requested_wait_is_used_but_capped(clock):
    fetcher, _ = fake_web(
        httpx.Response(429, headers={"Retry-After": "3600"}),
        httpx.Response(429, headers={"Retry-After": "5"}),
        httpx.Response(200, json=[]),
    )
    fetcher.get_json(LEVER)
    assert clock.waits == [fetch.MAX_RETRY_AFTER, 5]


def test_oversized_reply_is_refused(monkeypatch):
    monkeypatch.setattr(fetch, "MAX_BYTES", 10)
    fetcher, _ = fake_web(httpx.Response(200, content=b"[" + b"1," * 10 + b"1]"))
    with pytest.raises(fetch.FetchError, match="larger than"):
        fetcher.get_json(LEVER)


def test_unchanged_file_is_not_downloaded():
    fetcher, requests = fake_web(httpx.Response(304))
    assert fetcher.get_json(LEVER, etag='"v1"') is None
    assert requests[0].headers["If-None-Match"] == '"v1"'


def test_reply_that_is_not_json_is_refused():
    fetcher, _ = fake_web(httpx.Response(200, content=b"<html>maintenance</html>"))
    with pytest.raises(fetch.FetchError, match="not JSON"):
        fetcher.get_json(LEVER)


def test_one_request_per_second_per_website(clock):
    fetcher, _ = fake_web(*[httpx.Response(200, json=[])] * 3)
    fetcher.get_json(LEVER)
    fetcher.get_json(GREENHOUSE)  # another website: no wait
    fetcher.get_json(LEVER)
    assert clock.waits == [fetch.MIN_GAP_SECONDS]


def test_reachable_means_any_answer_and_only_allowed_sites_are_asked():
    fetcher, requests = fake_web(httpx.Response(404), httpx.ConnectError("down"))
    assert fetcher.reachable("api.lever.co") is True  # even an error page is an answer
    assert fetcher.reachable("api.ashbyhq.com") is False
    assert [request.method for request in requests] == ["HEAD", "HEAD"]
    with pytest.raises(fetch.FetchError, match="address not allowed"):
        fetcher.reachable("simplify.jobs")


def test_no_answer_at_all_is_reported_as_unreachable_with_the_blocking_hint():
    fetcher, _ = fake_web(*[httpx.ConnectError("down")] * 4)
    with pytest.raises(fetch.Unreachable, match="network may be blocking it"):
        fetcher.get_json(LEVER)
    fetcher, _ = fake_web(*[httpx.Response(503)] * 4)
    with pytest.raises(fetch.FetchError) as error:
        fetcher.get_json(LEVER)
    assert not isinstance(error.value, fetch.Unreachable)  # the site answered; it is just failing
