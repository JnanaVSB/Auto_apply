"""The one shared web download: allowed hosts only, polite, size-limited, with retries."""

import hashlib
import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

# The only places the app downloads from; anything else (including simplify.jobs) is refused.
COMMON_CRAWL = "index.commoncrawl.org"  # only the phone-book refresh reads it, never a collection
COLLECT_HOSTS = frozenset({"raw.githubusercontent.com", "boards-api.greenhouse.io", "api.lever.co", "api.ashbyhq.com"})
ALLOWED_HOSTS = COLLECT_HOSTS | {COMMON_CRAWL}
# Files job scoring downloads once (download_file): only these downloads may follow a redirect, and only between these
# hosts. huggingface.co hands its large files to its storage address, us.aws.cdn.hf.co.
SETUP_HOSTS = frozenset({"huggingface.co", "us.aws.cdn.hf.co", "www.onetcenter.org"})
MAX_REDIRECTS = 2
REDIRECTS = frozenset({301, 302, 303, 307, 308})
USER_AGENT = "auto_apply personal job search"
TIMEOUT_SECONDS = 20
MIN_GAP_SECONDS = 1.0  # between the starts of two requests to the same website, at one request per second
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


class _TryAgain(Exception):
    """A setup download that may work on the next try: the site was busy, or the file arrived incomplete."""


class Fetcher:
    """Downloads JSON politely. Keeps one connection pool and each website's next free start time."""

    def __init__(self, transport: httpx.BaseTransport | None = None, per_second: int = 1) -> None:
        self._client = httpx.Client(
            transport=transport, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_SECONDS, follow_redirects=False
        )
        self._next_start: dict[str, float] = {}  # per website: the earliest time its next request may start
        self._spacing = threading.Lock()  # several downloads share one Fetcher at once
        self.per_second = per_second  # new requests per second to one job site (requests_per_second in the settings)

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

    def download_file(self, url: str, path: Path, size: int, sha256: str) -> None:
        """Save one setup file all-or-nothing: only a file of exactly `size` bytes with the `sha256` fingerprint
        reaches `path`; anything else is refused and leaves no file."""
        partial = path.with_name(f"{path.name}.partial")
        waits = iter(RETRY_WAITS)
        try:
            while True:
                try:
                    digest = self._save(url, partial, size)
                    break
                except (httpx.RequestError, _TryAgain) as error:  # RequestError includes timeouts
                    wait = next(waits, None)
                    if wait is None:
                        reason = (
                            error if isinstance(error, _TryAgain) else f"connection failed ({type(error).__name__})"
                        )
                        raise FetchError(
                            f"{reason}; gave up after {len(RETRY_WAITS)} retries, try again later"
                        ) from None
                    time.sleep(wait)
            if digest != sha256:
                raise FetchError("the downloaded file did not match its fingerprint, so it was refused")
            partial.replace(path)
        finally:
            partial.unlink(missing_ok=True)  # after an error, Ctrl+C or a refusal

    def _save(self, url: str, partial: Path, size: int) -> str:
        """Download to `partial`, following at most MAX_REDIRECTS redirects between setup hosts; return the SHA-256."""
        for _ in range(MAX_REDIRECTS + 1):
            _setup_host(url)
            with self._client.stream("GET", url) as response:
                status = response.status_code
                if status in REDIRECTS and "Location" in response.headers:
                    url = str(response.url.join(response.headers["Location"]))
                    continue
                if status == 429 or status >= 500:
                    raise _TryAgain(f"busy or failing (status {status})")
                if status != 200:
                    raise FetchError(_refusal(status))
                digest, received = hashlib.sha256(), 0
                with partial.open("wb") as file:
                    for chunk in response.iter_bytes():
                        received += len(chunk)
                        if received > size:
                            raise FetchError("the file is larger than expected, so it was refused")
                        digest.update(chunk)
                        file.write(chunk)
                if received < size:
                    raise _TryAgain("the file arrived incomplete")
                return digest.hexdigest()
        raise FetchError(f"sent on more than {MAX_REDIRECTS} times, so it was refused")

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
        gap = COMMON_CRAWL_GAP_SECONDS if host == COMMON_CRAWL else MIN_GAP_SECONDS / self.per_second
        with self._spacing:
            start = max(time.monotonic(), self._next_start.get(host, 0.0))
            self._next_start[host] = start + gap
        if (wait := start - time.monotonic()) > 0:
            time.sleep(wait)


class Guarded:
    """A Fetcher that calls `check` before every request; `check` raises to stop (the dashboard is closing, or a
    collection is waiting for a phone-book refresh to pause)."""

    def __init__(self, fetcher: "Fetcher | Guarded", check: Callable[[], None]) -> None:
        self._fetcher, self._check = fetcher, check
        self.per_second = fetcher.per_second

    def reachable(self, host: str) -> bool:
        self._check()
        return self._fetcher.reachable(host)

    def get_json(self, url: str, etag: str | None = None) -> tuple[Any, str | None] | None:
        self._check()
        return self._fetcher.get_json(url, etag)

    def get_text(self, url: str) -> str:
        self._check()
        return self._fetcher.get_text(url)


def _allowed_host(url: str) -> str:
    try:
        parsed = httpx.URL(url)  # the same parser httpx uses for the request itself
    except httpx.InvalidURL:
        parsed = None
    if parsed is None or parsed.scheme != "https" or parsed.host not in ALLOWED_HOSTS or parsed.userinfo:
        raise FetchError("address not allowed (only https to the job lists, job-board APIs and Common Crawl's index)")
    return parsed.host


def _setup_host(url: str) -> None:
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        parsed = None
    if parsed is None or parsed.scheme != "https" or parsed.host not in SETUP_HOSTS or parsed.userinfo:
        where = parsed.host if parsed is not None and parsed.host else "an unknown address"
        raise FetchError(f"it was sent to {where}, which is not on the approved list; nothing was downloaded")


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
