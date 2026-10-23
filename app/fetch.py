"""The one shared web download: allowed hosts only, polite, size-limited, with retries."""

import json
import threading
import time
from typing import Any

import httpx

# The only places the app downloads from; anything else (including simplify.jobs) is refused.
COMMON_CRAWL = "index.commoncrawl.org"  # only the phone-book refresh reads it, never a collection
COLLECT_HOSTS = frozenset({"raw.githubusercontent.com", "boards-api.greenhouse.io", "api.lever.co", "api.ashbyhq.com"})
ALLOWED_HOSTS = COLLECT_HOSTS | {COMMON_CRAWL}
USER_AGENT = "auto_apply personal job search"
TIMEOUT_SECONDS = 20
MIN_GAP_SECONDS = 1.0  # between the starts of two requests to the same website
RETRY_WAITS = (2, 4, 8)
MAX_RETRY_AFTER = 60
# Common Crawl's index asks for pauses between calls and is often overloaded: slower, with longer retry waits.
COMMON_CRAWL_GAP_SECONDS = 5.0
COMMON_CRAWL_RETRY_WAITS = (30, 60, 120)
MAX_BYTES = 50 * 1024 * 1024
REACH_TIMEOUT_SECONDS = 10
BLOCKED_HINT = "the site may be down, or your network may be blocking it"


class FetchError(Exception):
    """A download failed. The message names the real reason; it never includes personal data."""


class NotFound(FetchError):
    """The site says the address does not exist (404), e.g. a company closed its job board."""


class Unreachable(FetchError):
    """No answer at all: the connection failed or timed out on every try."""


class Fetcher:
    """Downloads JSON politely. Keeps one connection pool and each website's next free start time."""

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self._client = httpx.Client(
            transport=transport, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_SECONDS, follow_redirects=False
        )
        self._next_start: dict[str, float] = {}  # per website: the earliest time its next request may start
        self._spacing = threading.Lock()  # several downloads share one Fetcher at once

    def close(self) -> None:
        self._client.close()

    def reachable(self, host: str) -> bool:
        """One quick request to an allowed site; any answer counts, even an error page."""
        _allowed_host(f"https://{host}/")
        self._space_out(host)
        try:
            self._client.head(f"https://{host}/", timeout=REACH_TIMEOUT_SECONDS)
        except httpx.RequestError:
            return False
        return True

    def get_json(self, url: str, etag: str | None = None) -> tuple[Any, str | None] | None:
        """Return (data, version tag), or None when the file is unchanged since `etag`."""
        result = self._get(url, etag)
        return None if result is None else (_parse_json(result[0]), result[1])

    def get_text(self, url: str) -> str:
        """Return a reply as text, e.g. Common Crawl's index lines; anything that is not UTF-8 is replaced."""
        return self._get(url, None)[0].decode("utf-8", errors="replace")

    def _get(self, url: str, etag: str | None) -> tuple[bytes, str | None] | None:
        host = _allowed_host(url)
        headers = {"If-None-Match": etag} if etag else {}
        retry_waits = COMMON_CRAWL_RETRY_WAITS if host == COMMON_CRAWL else RETRY_WAITS
        waits = iter(retry_waits)
        while True:
            self._space_out(host)
            try:
                with self._client.stream("GET", url, headers=headers) as response:
                    status = response.status_code
                    if status == 304:
                        return None
                    if status == 200:
                        return _read_limited(response), response.headers.get("ETag")
                    if status == 404:
                        raise NotFound("not found (404)")
                    if status != 429 and status < 500:
                        raise FetchError(_refusal(status))
                    reason, site_wait, answered = f"busy or failing (status {status})", _retry_after(response), True
            except httpx.TimeoutException:
                reason, site_wait, answered = "timed out", None, False
            except httpx.RequestError as error:  # dropped connection, broken compression, ...
                reason, site_wait, answered = f"connection failed ({type(error).__name__})", None, False
            wait = next(waits, None)
            if wait is None:
                gave_up = f"{reason}; gave up after {len(retry_waits)} retries"
                raise FetchError(gave_up) if answered else Unreachable(f"{gave_up} ({BLOCKED_HINT})")
            time.sleep(wait if site_wait is None else site_wait)

    def _space_out(self, host: str) -> None:
        """Wait for this website's turn: request starts are a gap apart, however many downloads ask."""
        gap = COMMON_CRAWL_GAP_SECONDS if host == COMMON_CRAWL else MIN_GAP_SECONDS
        with self._spacing:
            start = max(time.monotonic(), self._next_start.get(host, 0.0))
            self._next_start[host] = start + gap
        if (wait := start - time.monotonic()) > 0:
            time.sleep(wait)


def _allowed_host(url: str) -> str:
    try:
        parsed = httpx.URL(url)  # the same parser httpx uses for the request itself
    except httpx.InvalidURL:
        parsed = None
    if parsed is None or parsed.scheme != "https" or parsed.host not in ALLOWED_HOSTS or parsed.userinfo:
        raise FetchError("address not allowed (only https to the job lists, job-board APIs and Common Crawl's index)")
    return parsed.host


def _refusal(status: int) -> str:
    if 300 <= status < 400:
        return f"moved to another address (status {status})"
    return f"refused (status {status})"


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After", "")  # the date form is rare and ignored
    return min(int(value), MAX_RETRY_AFTER) if value.isdigit() else None


def _read_limited(response: httpx.Response) -> bytes:
    body = bytearray()
    for chunk in response.iter_bytes():
        body += chunk
        if len(body) > MAX_BYTES:
            raise FetchError(f"reply larger than {MAX_BYTES // 2**20} MB, refused")
    return bytes(body)


def _parse_json(body: bytes) -> Any:
    try:
        return json.loads(body)
    except (ValueError, RecursionError):  # RecursionError: absurdly deep nesting
        raise FetchError("returned data that is not JSON") from None
