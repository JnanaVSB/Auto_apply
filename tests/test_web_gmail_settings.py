"""Settings → Gmail: your Google client, Connect with a one-time check, Google's answer, Disconnect (a fake Google)."""

import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from helpers import csrf, log_in, one
from test_gmail_client import FakeGoogle

from app.gmail.client import CLIENT_ID, CLIENT_SECRET, REFRESH
from app.keychain import get_secret, set_secret
from app.web import gmail_settings

GOOD_ID = "123-made-up.apps.googleusercontent.com"
LASTING = "made-up-lasting-sign-in"
CODE = "made-up-one-time-code"
SECRET = "made-up-secret"  # noqa: S105 (a made-up test value)


@pytest.fixture
def google(client):
    """A fake Google for the dashboard: a token answer with a lasting sign-in, and revoke."""
    fake = FakeGoogle({"/token": [{"refresh_token": LASTING, "access_token": "a"}], "/revoke": [{}]})
    client.app.state.gmail_transport = httpx.MockTransport(fake)
    return fake


@pytest.fixture
def client(client):
    log_in(client)
    return client


def post(client, address, **form):
    return client.post(address, data=form | {"csrf_token": csrf(client)}, follow_redirects=False)


def save_client(client):
    return post(client, "/settings/gmail/client", client_id=GOOD_ID, client_secret=SECRET)


def connect(client) -> dict:
    """Press Connect; Google's address and what it was sent."""
    reply = client.get("/settings/gmail/connect", follow_redirects=False)
    assert reply.status_code == 303
    address = urlsplit(reply.headers["location"])
    return {"host": address.netloc, "path": address.path} | {k: v[0] for k, v in parse_qs(address.query).items()}


def logged_out(client, address, **params):
    """A request without the dashboard's login cookie, as Google's redirect arrives (the cookie is SameSite=Strict)."""
    cookies = dict(client.cookies)
    client.cookies.clear()
    try:
        return client.get(address, params=params, follow_redirects=False)
    finally:
        client.cookies.update(cookies)


def back_from_google(client, **params):
    return logged_out(client, gmail_settings.CALLBACK, **params)


def test_connect_needs_your_google_client_first(client):
    reply = client.get("/settings/gmail/connect", follow_redirects=False)
    assert reply.headers["location"] == "/settings/gmail?done=no-client"


@pytest.mark.parametrize(
    "client_id, client_secret",
    [("123-made-up.example.com", "s"), (GOOD_ID, "has space"), (GOOD_ID, ""), ("x" * 190 + GOOD_ID, "s"),
     (GOOD_ID, "s" * 201)],
)  # fmt: skip
def test_a_client_that_isn_t_google_s_shape_is_refused_and_nothing_saved(client, client_id, client_secret):
    reply = post(client, "/settings/gmail/client", client_id=client_id, client_secret=client_secret)
    assert reply.status_code == 400 and 'role="alert"' in reply.text
    assert get_secret(CLIENT_ID) is None and get_secret(CLIENT_SECRET) is None


def test_your_client_is_saved_in_the_keychain_and_never_shown(client):
    assert save_client(client).headers["location"] == "/settings/gmail?done=client"
    assert (get_secret(CLIENT_ID), get_secret(CLIENT_SECRET)) == (GOOD_ID, SECRET)
    text = client.get("/settings/gmail?done=client").text
    assert "Google client saved" in text and GOOD_ID not in text and SECRET not in text


def test_connect_sends_you_to_google_read_only_with_a_one_time_check(client):
    save_client(client)
    sent = connect(client)
    pending = client.app.state.gmail_pending
    challenge = base64.urlsafe_b64encode(hashlib.sha256(pending["verifier"].encode()).digest()).decode().rstrip("=")
    assert (sent["host"], sent["path"]) == ("accounts.google.com", "/o/oauth2/v2/auth")
    assert sent == sent | {
        "client_id": GOOD_ID, "redirect_uri": "http://127.0.0.1:8765/settings/gmail/callback", "response_type": "code",
        "scope": "https://www.googleapis.com/auth/gmail.readonly", "access_type": "offline", "prompt": "consent",
        "state": pending["state"], "code_challenge": challenge, "code_challenge_method": "S256",
    }  # fmt: skip


def test_google_s_answer_saves_the_lasting_sign_in_once(client, conn, google):
    save_client(client)
    state = connect(client)["state"]
    reply = back_from_google(client, state=state, code=CODE)
    assert reply.status_code == 200 and "Gmail connected" in reply.text and 'href="/settings/gmail"' in reply.text
    assert CODE not in reply.text and LASTING not in reply.text and state not in reply.text
    assert get_secret(REFRESH) == LASTING and one(conn, "SELECT connected_on FROM gmail_sync")[0]
    sent = parse_qs(google.requests[-1].content.decode())
    assert sent["code"] == [CODE] and sent["code_verifier"] and sent["grant_type"] == ["authorization_code"]
    again = back_from_google(client, state=state, code=CODE)
    assert again.status_code == 400 and "no longer valid" in again.text


def test_a_wrong_state_is_refused_and_ends_the_waiting_connect(client, google):
    save_client(client)
    state = connect(client)["state"]
    assert back_from_google(client, state="guessed", code=CODE).status_code == 400
    assert client.app.state.gmail_pending is None
    assert back_from_google(client, state=state, code=CODE).status_code == 400
    assert get_secret(REFRESH) is None and not google.requests


def test_an_answer_after_ten_minutes_is_refused(client, google, monkeypatch):
    save_client(client)
    state = connect(client)["state"]
    started = client.app.state.gmail_pending["created"]
    monkeypatch.setattr(gmail_settings, "_now", lambda: started + 11 * 60)
    assert back_from_google(client, state=state, code=CODE).status_code == 400
    assert get_secret(REFRESH) is None


def test_a_cancelled_sign_in_saves_nothing(client, google):
    save_client(client)
    reply = back_from_google(client, state=connect(client)["state"], error="access_denied")
    assert reply.status_code == 400 and "cancelled" in reply.text and get_secret(REFRESH) is None


@pytest.mark.parametrize(
    "answer, words",
    [(400, "Google refused the sign-in (400)"), ({"access_token": "a"}, "didn&#39;t return a lasting sign-in")],
)
def test_google_refusing_the_code_saves_nothing(client, google, answer, words):
    google.replies["/token"] = [answer]
    save_client(client)
    reply = back_from_google(client, state=connect(client)["state"], code=CODE)
    assert reply.status_code == 400 and words in reply.text and get_secret(REFRESH) is None


@pytest.mark.parametrize("address", ["/settings/gmail", "/settings/gmail/callbackx", "/settings/gmail/connect"])
def test_only_the_exact_callback_works_without_logging_in(client, address):
    reply = logged_out(client, address)
    assert (reply.status_code, reply.headers["location"]) == (303, "/login")


def test_disconnect_tells_google_and_forgets_the_sign_in(client, conn, google):
    set_secret(REFRESH, LASTING)
    with conn:
        conn.execute("INSERT INTO gmail_sync (id, connected_on) VALUES (1, '2026-11-04T10:00:00+00:00')")
    assert "Google asks you to connect again" in client.get("/settings/gmail").text
    reply = post(client, "/settings/gmail/disconnect")
    assert reply.headers["location"] == "/settings/gmail?done=disconnected"
    assert get_secret(REFRESH) is None and one(conn, "SELECT connected_on FROM gmail_sync")[0] is None
    assert google.requests[-1].url.path == "/revoke"


def test_disconnect_forgets_the_sign_in_even_when_google_can_t_be_told(client, google):
    google.replies["/revoke"] = [httpx.ConnectError("no route to host")]
    set_secret(REFRESH, LASTING)
    assert post(client, "/settings/gmail/disconnect").headers["location"] == "/settings/gmail?done=disconnected-here"
    assert get_secret(REFRESH) is None
    assert "security page" in client.get("/settings/gmail?done=disconnected-here").text


def test_disconnect_needs_the_page_s_token(client):
    set_secret(REFRESH, LASTING)
    reply = client.post("/settings/gmail/disconnect", follow_redirects=False)
    assert reply.status_code == 403 and get_secret(REFRESH) == LASTING


def test_the_gmail_pages_keep_the_same_safety_rules(client):
    rules = client.get("/").headers["content-security-policy"]
    assert client.get("/settings/gmail").headers["content-security-policy"] == rules
    assert back_from_google(client, state="x").headers["content-security-policy"] == rules


def test_sync_now_needs_the_page_s_token_and_starts_a_pass(client, monkeypatch):
    pressed = []
    monkeypatch.setattr(client.app.state.mailer, "wake", lambda: pressed.append(1))
    assert client.post("/settings/gmail/sync", follow_redirects=False).status_code == 403 and not pressed
    assert post(client, "/settings/gmail/sync").headers["location"] == "/settings/gmail?done=syncing"
    assert pressed == [1] and "Syncing now" in client.get("/settings/gmail?done=syncing").text


def test_the_page_shows_the_address_gmail_gives_and_the_last_sync(client, conn, google):
    google.replies["/profile"] = [{"emailAddress": "jordan@example.com", "historyId": "1"}]
    save_client(client)
    set_secret(REFRESH, LASTING)
    text = client.get("/settings/gmail").text
    assert "jordan@example.com" in text and "Not synced yet." in text and "Sync now" in text
    with conn:
        conn.execute("INSERT INTO gmail_sync (id, last_sync_on, last_result) VALUES (1, ?, 'Read 2 new e-mails.')",
                     ("2026-11-04T10:00:00+00:00",))  # fmt: skip
    client.app.state.mailer.notice = "A Gmail sync is running in another dashboard; it carries on there."
    text = client.get("/settings/gmail").text
    assert "Read 2 new e-mails." in text and "Latest try: A Gmail sync is running" in text


def test_an_address_gmail_can_t_give_is_said_plainly(client, google):
    google.replies["/token"] = [httpx.Response(400, json={"error": "invalid_grant"})]
    save_client(client)
    set_secret(REFRESH, LASTING)
    assert "The address can't be read right now: Gmail needs connecting again" in client.get("/settings/gmail").text
