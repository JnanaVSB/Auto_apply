"""Downloads, tested against httpx's built-in fake web layer; nothing here reaches the internet."""

import hashlib
import threading
import time
from itertools import pairwise

import httpx
import pytest

from app import fetch

LEVER = "https://api.lever.co/v0/postings/acme?mode=json"
GREENHOUSE = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"


@pytest.fixture(autouse=True)
def clock(no_waiting):
    return no_waiting


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


def test_downloads_running_at_the_same_time_still_start_a_gap_apart(monkeypatch):
    monkeypatch.setattr(fetch, "time", time)  # real waiting, with a short gap
    monkeypatch.setattr(fetch, "MIN_GAP_SECONDS", 0.05)
    starts = []

    def answer(request):
        starts.append(time.monotonic())
        return httpx.Response(200, json=[])

    fetcher = fetch.Fetcher(httpx.MockTransport(answer))
    downloads = [threading.Thread(target=fetcher.get_json, args=(LEVER,)) for _ in range(4)]
    for download in downloads:
        download.start()
    for download in downloads:
        download.join()
    gaps = [later - earlier for earlier, later in pairwise(sorted(starts))]
    assert len(gaps) == 3
    assert min(gaps) >= 0.045  # a little slack for coarse system clocks


def test_more_requests_per_second_means_a_shorter_gap(clock):
    fetcher, _ = fake_web(*[httpx.Response(200, json=[])] * 2)
    fetcher.per_second = 4
    fetcher.get_json(LEVER)
    fetcher.get_json(LEVER)
    assert clock.waits == [0.25]


def test_common_crawl_is_asked_slowly_and_its_lines_are_read_as_text(clock):
    index = "https://index.commoncrawl.org/CC-MAIN-2026-40-index?url=jobs.lever.co/*&output=json&fl=url"
    fetcher, _ = fake_web(*[httpx.Response(200, text='{"url": "a"}\n{"url": "b"}\n')] * 2)
    assert fetcher.get_text(index) == '{"url": "a"}\n{"url": "b"}\n'
    fetcher.get_text(index)
    assert clock.waits == [fetch.COMMON_CRAWL_GAP_SECONDS]


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


CONTENT = b"fake model bytes"
SHA256 = hashlib.sha256(CONTENT).hexdigest()
MODEL = "https://huggingface.co/fake/model.onnx"


def download(tmp_path, *replies):
    """Download the fake setup file through `replies`; returns its path and the requests made."""
    fetcher, requests = fake_web(*replies)
    path = tmp_path / "model.onnx"
    fetcher.download_file(MODEL, path, len(CONTENT), SHA256)
    return path, requests


def moved(location):
    return httpx.Response(302, headers={"Location": location})


def test_setup_file_follows_redirects_between_approved_hosts(tmp_path):
    path, requests = download(
        tmp_path,
        httpx.Response(307, headers={"Location": "/api/resolve-cache/model.onnx"}),  # same site, short form
        moved("https://us.aws.cdn.hf.co/xet/abc?signature=1"),
        httpx.Response(200, content=CONTENT),
    )
    assert path.read_bytes() == CONTENT
    assert [str(request.url) for request in requests] == [
        MODEL,
        "https://huggingface.co/api/resolve-cache/model.onnx",
        "https://us.aws.cdn.hf.co/xet/abc?signature=1",
    ]
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize(
    ("location", "host"),
    [
        ("https://cdn.example.net/model.onnx", "cdn.example.net"),
        ("http://us.aws.cdn.hf.co/model.onnx", "us.aws.cdn.hf.co"),  # not https
        ("https://api.lever.co/model.onnx", "api.lever.co"),  # a job site is not a setup host
    ],
)
def test_setup_file_refuses_a_redirect_elsewhere(tmp_path, location, host):
    with pytest.raises(fetch.FetchError, match=f"sent to {host}, which is not on the approved list"):
        download(tmp_path, moved(location))
    assert list(tmp_path.iterdir()) == []


def test_setup_file_stops_after_two_redirects(tmp_path):
    with pytest.raises(fetch.FetchError, match="sent on more than 2 times"):
        download(tmp_path, moved(MODEL), moved(MODEL), moved(MODEL))


def test_setup_file_with_the_wrong_fingerprint_is_refused(tmp_path):
    with pytest.raises(fetch.FetchError, match="did not match its fingerprint"):
        download(tmp_path, httpx.Response(200, content=b"other model bytes"[: len(CONTENT)]))
    assert list(tmp_path.iterdir()) == []


def test_setup_file_larger_than_expected_is_refused(tmp_path):
    with pytest.raises(fetch.FetchError, match="larger than expected"):
        download(tmp_path, httpx.Response(200, content=CONTENT + b" and more"))
    assert list(tmp_path.iterdir()) == []


def test_busy_site_or_incomplete_file_is_tried_again(tmp_path, clock):
    path, _ = download(
        tmp_path, httpx.Response(503), httpx.Response(200, content=CONTENT[:5]), httpx.Response(200, content=CONTENT)
    )
    assert path.read_bytes() == CONTENT
    assert clock.waits == [2, 4]


def test_setup_file_gives_up_after_three_retries(tmp_path):
    with pytest.raises(fetch.FetchError, match="connection failed .ConnectError.; gave up after 3 retries, try again"):
        download(tmp_path, *[httpx.ConnectError("no route to host")] * 4)
    assert list(tmp_path.iterdir()) == []


def test_interrupted_setup_download_leaves_no_file(tmp_path):
    def stopped_halfway():
        yield CONTENT[:5]
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        download(tmp_path, httpx.Response(200, content=stopped_halfway()))
    assert list(tmp_path.iterdir()) == []
