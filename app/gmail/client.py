"""A small read-only Gmail client over httpx: a fresh access token from the refresh token, then search, history, one
message's headers and preview, and (for the local model only) a message's text. Errors are plain words: never a token,
never an e-mail's text, never Google's answer."""

import base64
import functools
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.sources import html_to_text

TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 (an address, not a password)
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
CLIENT_ID, CLIENT_SECRET, REFRESH = "gmail-client-id", "gmail-client-secret", "gmail-refresh-token"  # keychain names
WAITS = (2, 4, 8)  # seconds between tries when Google is busy (429 or 5xx); then the next sync tries again
MAX_WAIT = 60  # the most a Retry-After is waited
MAX_PAGES = 50  # pages per search or change list; beyond it the next pass reads on (the change list's position stops
# at the last change read)
BUSY = "Gmail is busy; the next sync tries again."
BODY_LIMIT = 4000  # characters of an e-mail's text the local model may read
UNEXPECTED = "Gmail answered in an unexpected way; nothing was changed."
_sleep = time.sleep  # tests replace it


class GmailError(Exception):
    """Gmail can't be used right now; the message says why in plain words."""


class Reconnect(GmailError):
    """Google no longer accepts the saved sign-in."""

    def __init__(self) -> None:
        super().__init__("Gmail needs connecting again (Settings → Gmail).")


class Gone(GmailError):
    """That e-mail is no longer in Gmail (deleted, a replaced draft, purged spam): it is skipped."""


class HistoryTooOld(GmailError):
    """Gmail no longer has the changes since the saved position: a fresh search is needed."""


def _shaped(method: Callable) -> Callable:
    """An answer of an unknown shape (a field missing or of the wrong type) stops with plain words, never a crash."""

    @functools.wraps(method)
    def read(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except (AttributeError, KeyError, TypeError, ValueError):
            raise GmailError(UNEXPECTED) from None

    return read


class Gmail:
    def __init__(
        self, client_id: str, client_secret: str, refresh_token: str, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._sign_in = {"client_id": client_id, "client_secret": client_secret, "refresh_token": refresh_token,
                         "grant_type": "refresh_token"}  # fmt: skip
        self._client = httpx.Client(transport=transport, timeout=30)
        self._access, self._expires = "", 0.0  # in memory only
        self._budget = float(sum(WAITS))  # seconds of waiting for a busy Google, shared by every request of this pass

    def __enter__(self) -> "Gmail":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    @_shaped
    def profile(self) -> tuple[str, str]:
        """The account's address and its current history position."""
        found = self._get("/profile", {})
        return str(found.get("emailAddress", "")), str(found.get("historyId", ""))

    @_shaped
    def search(self, query: str) -> list[str]:
        """The ids of the messages a Gmail search finds, every page."""
        params = {"q": query, "maxResults": 100, "fields": "messages/id,nextPageToken"}
        return [message["id"] for page in self._pages("/messages", params)[0] for message in page.get("messages", [])]

    @_shaped
    def history(self, start: str) -> tuple[list[str], str]:
        """The ids of the messages added since `start`, and the newest position Gmail reports."""
        ids, newest = [], start
        pages, more = self._pages("/history", {"startHistoryId": start, "historyTypes": "messageAdded"})
        for page in pages:
            ids += [
                added["message"]["id"] for entry in page.get("history", []) for added in entry.get("messagesAdded", [])
            ]
            newest = str(page.get("historyId", newest))
        if more:  # stopped early: the next pass reads on from the last change read here
            newest = next((str(entry["id"]) for page in reversed(pages) for entry in reversed(page.get("history", []))),
                          start)  # fmt: skip
        return ids, newest

    @_shaped
    def message(self, message_id: str) -> dict:
        """One message's thread, time (UTC), sender, subject, Gmail's preview and labels (SENT, SPAM, …); never its
        text. The time is when Gmail got it, else its Date line, else now."""
        found = self._get(
            f"/messages/{message_id}", {"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]}
        )
        headers = {header["name"].lower(): header["value"] for header in found.get("payload", {}).get("headers", [])}
        if "internalDate" in found:
            received = datetime.fromtimestamp(int(found["internalDate"]) / 1000, UTC)
        else:
            try:
                received = parsedate_to_datetime(headers.get("date", "")).astimezone(UTC)
            except (TypeError, ValueError):
                received = datetime.now(UTC)
        return {
            "id": found.get("id", message_id),
            "thread_id": found.get("threadId", ""),
            "received_on": received.isoformat(timespec="seconds"),
            "from": headers.get("from", ""),
            "subject": headers.get("subject", ""),
            "snippet": found.get("snippet", ""),
            "labels": [str(label) for label in found.get("labelIds", [])],
        }

    @_shaped
    def body(self, message_id: str) -> str:
        """The message's text, cut to BODY_LIMIT, for the local model only: it is never saved anywhere."""
        payload = self._get(f"/messages/{message_id}", {"format": "full"}).get("payload", {})
        plain, html = _part(payload, "text/plain"), _part(payload, "text/html")
        text = _decode(plain) if plain is not None else html_to_text(_decode(html)) if html is not None else ""
        return text[:BODY_LIMIT]

    def _pages(self, path: str, params: dict) -> tuple[list[dict], bool]:
        """Up to MAX_PAGES pages, and whether there were more."""
        pages, token = [], None
        while len(pages) < MAX_PAGES:
            page = self._get(path, params | ({"pageToken": token} if token else {}))
            pages.append(page)
            token = page.get("nextPageToken")
            if not token:
                return pages, False
        return pages, True

    def _wait(self, reply: httpx.Response, tried: int) -> None:
        """Google is busy: wait its Retry-After (seconds, at most MAX_WAIT) or the next of WAITS, out of this pass's
        budget; past the tries or the budget, "busy" (the next sync tries again)."""
        after = reply.headers.get("retry-after", "")
        wait = min(int(after), MAX_WAIT) if after.isdigit() else WAITS[min(tried, len(WAITS) - 1)]
        if tried >= len(WAITS) or wait > self._budget:
            raise GmailError(BUSY)
        self._budget -= wait
        _sleep(wait)

    def _token(self) -> str:
        if self._access and self._expires - time.time() > 60:
            return self._access
        tried = 0
        while True:
            try:
                reply = self._client.post(TOKEN_URL, data=self._sign_in)
            except httpx.HTTPError:
                raise GmailError("Gmail could not be reached (no internet?); the next sync tries again.") from None
            if reply.status_code != 429 and reply.status_code < 500:
                break
            self._wait(reply, tried)
            tried += 1
        found = _json(reply) or {}
        if reply.status_code == 400 and found.get("error") == "invalid_grant":
            raise Reconnect
        if reply.status_code != 200 or "access_token" not in found:
            raise GmailError(
                f"Google refused the sign-in ({reply.status_code}); check the client ID and secret in Settings → Gmail."
            )
        self._access, self._expires = found["access_token"], time.time() + float(found.get("expires_in", 3600))
        return self._access

    def _get(self, path: str, params: dict) -> dict:
        refreshed, tried = False, 0
        while True:
            try:
                reply = self._client.get(
                    API + path, params=params, headers={"Authorization": f"Bearer {self._token()}"}
                )
            except httpx.HTTPError:
                raise GmailError("Gmail could not be reached (no internet?); the next sync tries again.") from None
            if reply.status_code == 401 and not refreshed:  # the access token ran out early: one new one
                self._access, refreshed = "", True
                continue
            if reply.status_code == 429 or reply.status_code >= 500:
                self._wait(reply, tried)
                tried += 1
                continue
            if reply.status_code == 404 and path.startswith("/messages/"):
                raise Gone("That e-mail is no longer in Gmail.")
            if reply.status_code == 404 and path == "/history":
                raise HistoryTooOld(
                    "Gmail no longer has the changes since the last sync; looking again from the start."
                )
            if reply.status_code != 200:
                raise GmailError(f"Gmail refused the request ({reply.status_code}); the next sync tries again.")
            found = _json(reply)
            if found is None:
                raise GmailError(UNEXPECTED)
            return found


def exchange(
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    verifier: str,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """The lasting sign-in (refresh token) for the one-time code Google sent back after you approved; refused unless
    the read-only permission was granted."""
    data = {"grant_type": "authorization_code", "code": code, "client_id": client_id, "client_secret": client_secret,
            "redirect_uri": redirect_uri, "code_verifier": verifier}  # fmt: skip
    try:
        with httpx.Client(transport=transport, timeout=30) as client:
            reply = client.post(TOKEN_URL, data=data)
    except httpx.HTTPError:
        raise GmailError("Google could not be reached (no internet?); press Connect again.") from None
    if reply.status_code != 200:
        raise GmailError(f"Google refused the sign-in ({reply.status_code}); press Connect again.")
    found = _json(reply) or {}
    if SCOPE not in str(found.get("scope", "")).split():  # you can untick it on Google's page
        raise GmailError("The read-only permission wasn't granted; press Connect again and tick it.")
    token = found.get("refresh_token")
    if not isinstance(token, str) or not token:
        raise GmailError("Google didn't return a lasting sign-in; press Connect again.")
    return token


def revoke(token: str, transport: httpx.BaseTransport | None = None) -> bool:
    """Ask Google to forget the sign-in; whether it confirmed (no internet: False)."""
    try:
        with httpx.Client(transport=transport, timeout=30) as client:
            return client.post(REVOKE_URL, data={"token": token}).status_code == 200
    except httpx.HTTPError:
        return False


def _json(reply: httpx.Response) -> dict[str, Any] | None:
    """The answer as a JSON object, or None when it is anything else."""
    try:
        found = reply.json()
    except ValueError:
        return None
    return found if isinstance(found, dict) else None


def _part(payload: dict, mime_type: str) -> dict | None:
    """The first part of this type, walking nested parts."""
    if payload.get("mimeType") == mime_type and "data" in payload.get("body", {}):
        return payload
    return next((found for part in payload.get("parts", []) if (found := _part(part, mime_type))), None)


def _decode(part: dict) -> str:
    """A part's text (Gmail sends it base64url-encoded); "" when it can't be decoded."""
    data = str(part["body"]["data"])
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")
    except ValueError:  # binascii.Error: broken base64
        return ""
