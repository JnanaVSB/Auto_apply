"""The one shared web download: allowed hosts only, polite, size-limited, with retries."""

import json
import time
from typing import Any

import httpx

# The only places the app downloads from; anything else (including simplify.jobs) is refused.
ALLOWED_HOSTS = frozenset({"raw.githubusercontent.com", "boards-api.greenhouse.io", "api.lever.co", "api.ashbyhq.com"})
USER_AGENT = "auto_apply personal job search"
TIMEOUT_SECONDS = 20
MIN_GAP_SECONDS = 1.0  # between two requests to the same website
RETRY_WAITS = (2, 4, 8)
MAX_RETRY_AFTER = 60
MAX_BYTES = 50 * 1024 * 1024


class FetchError(Exception):
    """A download failed. The message names the real reason; it never includes personal data."""


class Fetcher:
    """Downloads JSON politely. Keeps one connection pool and the time of the last request to each website."""

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self._client = httpx.Client(
            transport=transport, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_SECONDS, follow_redirects=False
        )
        self._last_request: dict[str, float] = {}

    def close(self) -> None:
        self._client.close()

    def get_json(self, url: str, etag: str | None = None) -> tuple[Any, str | None] | None:
        """Return (data, version tag), or None when the file is unchanged since `etag`."""
        host = _allowed_host(url)
        headers = {"If-None-Match": etag} if etag else {}
        waits = iter(RETRY_WAITS)
        while True:
            self._space_out(host)
            try:
                with self._client.stream("GET", url, headers=headers) as response:
                    status = response.status_code
                    if status == 304:
                        return None
                    if status == 200:
                        return _parse_json(_read_limited(response)), response.headers.get("ETag")
                    if status != 429 and status < 500:
                        raise FetchError(_refusal(status))
                    reason, site_wait = f"busy or failing (status {status})", _retry_after(response)
            except httpx.TimeoutException:
                reason, site_wait = "timed out", None
            except httpx.RequestError as error:  # dropped connection, broken compression, ...
                reason, site_wait = f"connection failed ({type(error).__name__})", None
            wait = next(waits, None)
            if wait is None:
                raise FetchError(f"{reason}; gave up after {len(RETRY_WAITS)} retries")
            time.sleep(wait if site_wait is None else site_wait)

    def _space_out(self, host: str) -> None:
        gap = self._last_request.get(host, -MIN_GAP_SECONDS) + MIN_GAP_SECONDS - time.monotonic()
        if gap > 0:
            time.sleep(gap)
        self._last_request[host] = time.monotonic()


def _allowed_host(url: str) -> str:
    try:
        parsed = httpx.URL(url)  # the same parser httpx uses for the request itself
    except httpx.InvalidURL:
        parsed = None
    if parsed is None or parsed.scheme != "https" or parsed.host not in ALLOWED_HOSTS or parsed.userinfo:
        raise FetchError("address not allowed (only https to the job-list and job-board APIs)")
    return parsed.host


def _refusal(status: int) -> str:
    if status == 404:
        return "not found (404)"
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
