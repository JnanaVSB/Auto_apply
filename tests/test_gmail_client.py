"""The Gmail client against a fake Google (httpx.MockTransport): nothing leaves this computer."""

import base64
import json
from datetime import UTC, datetime

import httpx
import pytest

from app.gmail import client
from app.gmail.client import Gmail, GmailError, HistoryTooOld, Reconnect

REFRESH, ACCESS = "made-up-refresh-token", "made-up-access-token"
SNIPPET = "Dear Jordan Example, your secret interview code is 4321"
TOKEN = {"access_token": ACCESS, "expires_in": 3600}


class FakeGoogle:
    """Answers each path with the next reply in its list (the last one repeats); records every request, at most 49."""

    def __init__(self, replies: dict):
        self.replies = {"/token": [TOKEN]} | replies
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert len(self.requests) < 50, "the client kept asking: no limit on tries"
        path = request.url.path.removeprefix("/gmail/v1/users/me")
        queue = self.replies[path]
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, int):
            return httpx.Response(reply, json={"error": {"message": SNIPPET, "token": ACCESS}})
        if isinstance(reply, httpx.Response):
            return reply
        return httpx.Response(200, json=reply)

    def calls(self, path: str) -> int:
        return sum(request.url.path.endswith(path) for request in self.requests)


@pytest.fixture
def waits(monkeypatch):
    found = []
    monkeypatch.setattr(client, "_sleep", found.append)
    return found


def gmail(fake: FakeGoogle) -> Gmail:
    return Gmail("made-up-client-id", "made-up-client-secret", REFRESH, httpx.MockTransport(fake))


def encoded(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def test_one_token_serves_several_calls_and_a_token_near_expiry_is_renewed(monkeypatch):
    fake = FakeGoogle({"/profile": [{"emailAddress": "jordan@example.com", "historyId": "77"}]})
    with gmail(fake) as box:
        assert box.profile() == ("jordan@example.com", "77")
        box.profile()
        assert fake.calls("/token") == 1
        monkeypatch.setattr(client.time, "time", lambda: datetime.now(UTC).timestamp() + 3590)
        box.profile()
    assert fake.calls("/token") == 2


def test_a_refused_refresh_token_asks_to_reconnect():
    fake = FakeGoogle({"/token": [httpx.Response(400, json={"error": "invalid_grant"})], "/profile": [{}]})
    with gmail(fake) as box, pytest.raises(Reconnect, match="connecting again"):
        box.profile()


def test_another_refusal_points_to_the_client_id_and_secret():
    fake = FakeGoogle({"/token": [httpx.Response(401, json={"error": "invalid_client"})], "/profile": [{}]})
    with gmail(fake) as box, pytest.raises(GmailError, match="Settings → Gmail"):
        box.profile()


def test_a_401_gets_a_new_token_and_one_more_try():
    fake = FakeGoogle({"/profile": [401, {"emailAddress": "jordan@example.com", "historyId": "1"}]})
    with gmail(fake) as box:
        assert box.profile()[0] == "jordan@example.com"
    assert fake.calls("/token") == 2


def test_a_busy_gmail_is_tried_again_after_a_wait(waits):
    fake = FakeGoogle({"/profile": [429, 429, {"emailAddress": "jordan@example.com", "historyId": "1"}]})
    with gmail(fake) as box:
        assert box.profile()[1] == "1"
    assert waits == [2, 4]


def test_a_gmail_busy_too_long_waits_for_the_next_sync(waits):
    fake = FakeGoogle({"/profile": [503] * 10 + [{"historyId": "1"}]})
    with gmail(fake) as box, pytest.raises(GmailError, match="busy"):
        box.profile()
    assert waits == list(client.WAITS) and fake.calls("/profile") == len(client.WAITS) + 1


@pytest.mark.parametrize(
    "reply, words",
    [(httpx.ConnectError("no route to host"), "could not be reached"),
     (httpx.Response(200, json=["not", "an", "object"]), "unexpected way"),
     (httpx.Response(200, text="<html>not json</html>"), "unexpected way")],
)  # fmt: skip
def test_an_unreachable_or_odd_gmail_says_so_in_plain_words(reply, words):
    with gmail(FakeGoogle({"/profile": [reply]})) as box, pytest.raises(GmailError, match=words):
        box.profile()


def test_search_and_history_follow_every_page():
    fake = FakeGoogle({
        "/messages": [{"messages": [{"id": "a"}, {"id": "b"}], "nextPageToken": "p2"}, {"messages": [{"id": "c"}]}],
        "/history": [
            {"history": [{"messagesAdded": [{"message": {"id": "d"}}]}], "historyId": "90", "nextPageToken": "h2"},
            {"history": [{"messagesAdded": [{"message": {"id": "e"}}]}, {}], "historyId": "95"},
        ],
    })  # fmt: skip
    with gmail(fake) as box:
        assert box.search("newer_than:30d") == ["a", "b", "c"]
        assert box.history("80") == (["d", "e"], "95")
    search = [request for request in fake.requests if request.url.path.endswith("/messages")]
    assert search[1].url.params["pageToken"] == "p2" and "nextPageToken" in search[0].url.params["fields"]


def test_a_history_gmail_no_longer_has_asks_for_a_fresh_search():
    with gmail(FakeGoogle({"/history": [404]})) as box, pytest.raises(HistoryTooOld):
        box.history("1")


def test_a_message_gives_its_headers_whatever_their_case_and_its_time_in_utc():
    found = {"id": "m1", "threadId": "t1", "internalDate": "1793790000000", "snippet": "We&#39;d like to talk",
             "labelIds": ["INBOX", "UNREAD"],
             "payload": {"headers": [{"name": "FROM", "value": "Acme <jobs@acme.example>"},
                                     {"name": "date", "value": "Wed, 4 Nov 2026"}]}}  # fmt: skip
    with gmail(FakeGoogle({"/messages/m1": [found]})) as box:
        assert box.message("m1") == {
            "id": "m1", "thread_id": "t1", "received_on": "2026-11-04T11:00:00+00:00",
            "from": "Acme <jobs@acme.example>", "subject": "", "snippet": "We&#39;d like to talk",
            "labels": ["INBOX", "UNREAD"],
        }  # fmt: skip


@pytest.mark.parametrize(
    "date, received",
    [("Wed, 04 Nov 2026 09:30:00 -0700", "2026-11-04T16:30:00+00:00"), ("not a date", None), ("", None)],
)
def test_a_message_gmail_gives_no_time_for_takes_its_date_line_else_now(date, received):
    found = {"id": "m1", "payload": {"headers": [{"name": "Date", "value": date}]}}
    with gmail(FakeGoogle({"/messages/m1": [found]})) as box:
        got = box.message("m1")["received_on"]
    if received:
        assert got == received
    else:  # now, never 1970
        assert abs(datetime.fromisoformat(got) - datetime.now(UTC)).total_seconds() < 60


@pytest.mark.parametrize("read", [lambda box: box.message("m1"), lambda box: box.body("m1")])
def test_an_e_mail_no_longer_in_gmail_is_gone_not_a_failure(read):
    with gmail(FakeGoogle({"/messages/m1": [404]})) as box, pytest.raises(client.Gone):
        read(box)


@pytest.mark.parametrize(
    "payload, text",
    [({"mimeType": "multipart/mixed", "parts": [{"mimeType": "multipart/alternative", "parts": [
        {"mimeType": "text/html", "body": {"data": encoded("<p>HTML</p>")}},
        {"mimeType": "text/plain", "body": {"data": encoded("Plain words")}}]}]}, "Plain words"),
     ({"mimeType": "text/html", "body": {"data": encoded("<p>We &amp; you</p><script>x()</script>")}}, "We & you"),
     ({"mimeType": "text/plain", "body": {"data": "@@@not base64@@@!"}}, ""),
     ({"mimeType": "text/plain", "body": {"data": encoded("a" * 5000)}}, "a" * client.BODY_LIMIT),
     ({"mimeType": "image/png", "body": {"data": encoded("x")}}, "")],
)  # fmt: skip
def test_the_body_is_the_plain_text_else_the_html_without_tags_cut_to_the_limit(payload, text):
    with gmail(FakeGoogle({"/messages/m1": [{"payload": payload}]})) as box:
        assert box.body("m1") == text


def test_only_google_s_two_addresses_are_used_and_only_sign_ins_are_posts():
    fake = FakeGoogle({"/profile": [{"historyId": "1"}], "/messages": [{}], "/messages/m1": [{"payload": {}}]})
    with gmail(fake) as box:
        box.profile(), box.search("x"), box.message("m1"), box.body("m1")
    fake.replies["/revoke"] = [{}]
    assert client.revoke(REFRESH, httpx.MockTransport(fake))
    assert {request.url.host for request in fake.requests} == {"oauth2.googleapis.com", "gmail.googleapis.com"}
    assert all((request.method == "POST") == (request.url.path in ("/token", "/revoke")) for request in fake.requests)


def test_revoke_says_whether_google_confirmed():
    assert not client.revoke(REFRESH, httpx.MockTransport(FakeGoogle({"/revoke": [400]})))
    assert not client.revoke(REFRESH, httpx.MockTransport(FakeGoogle({"/revoke": [httpx.ConnectError("down")]})))


@pytest.mark.parametrize(
    "replies",
    [{"/token": [httpx.Response(400, json={"error": "invalid_grant", "detail": SNIPPET})]},
     {"/token": [httpx.Response(500, json={"error": SNIPPET, "refresh": REFRESH})]},
     {"/profile": [403]}, {"/profile": [503]}, {"/profile": [httpx.Response(200, text=SNIPPET)]},
     {"/history": [404]}],
)  # fmt: skip
def test_no_error_shows_a_token_or_an_e_mail_s_text(replies, waits):
    fake = FakeGoogle({"/profile": [{}], "/history": [{}]} | replies)
    with gmail(fake) as box, pytest.raises(GmailError) as error:
        box.history("1") if "/history" in replies else box.profile()
    words = str(error.value) + json.dumps(error.value.args)
    assert REFRESH not in words and ACCESS not in words and "Jordan" not in words and "4321" not in words


@pytest.mark.parametrize(
    "replies, read",
    [({"/messages/m1": [{"payload": {"headers": [{"value": "no name"}]}}]}, lambda box: box.message("m1")),
     ({"/messages/m1": [{"internalDate": "yesterday"}]}, lambda box: box.message("m1")),
     ({"/history": [{"history": [{"messagesAdded": [{"no": "message"}]}]}]}, lambda box: box.history("1")),
     ({"/messages": [{"messages": "not a list"}]}, lambda box: box.search("x")),
     ({"/token": [{"access_token": ACCESS, "expires_in": "soon"}], "/profile": [{}]}, lambda box: box.profile())],
)  # fmt: skip
def test_an_answer_of_an_unknown_shape_stops_with_plain_words(replies, read):
    with gmail(FakeGoogle(replies)) as box, pytest.raises(GmailError, match="unexpected way"):
        read(box)


def test_the_one_time_code_is_exchanged_for_the_lasting_sign_in():
    fake = FakeGoogle({"/token": [{"refresh_token": REFRESH, "access_token": ACCESS, "scope": client.SCOPE}]})
    assert client.exchange("code", "id", "secret", "http://127.0.0.1:8765/cb", "verifier", httpx.MockTransport(fake))
    sent = dict(httpx.QueryParams(fake.requests[0].content.decode()))
    assert sent == {"grant_type": "authorization_code", "code": "code", "client_id": "id", "client_secret": "secret",
                    "redirect_uri": "http://127.0.0.1:8765/cb", "code_verifier": "verifier"}  # fmt: skip


@pytest.mark.parametrize(
    "answer, words",
    [(400, r"refused the sign-in \(400\)"), ({"access_token": ACCESS, "scope": client.SCOPE}, "lasting sign-in"),
     ({"refresh_token": REFRESH}, "read-only permission wasn't granted"),
     (httpx.ConnectError("down"), "could not be reached")],
)  # fmt: skip
def test_an_exchange_google_refuses_says_so_without_its_answer(answer, words):
    with pytest.raises(GmailError, match=words) as error:
        client.exchange(
            "code",
            "id",
            "secret",
            "http://127.0.0.1:8765/cb",
            "v",
            httpx.MockTransport(FakeGoogle({"/token": [answer]})),
        )
    assert SNIPPET not in str(error.value) and ACCESS not in str(error.value)


def test_a_busy_sign_in_address_is_waited_out_like_gmail(waits):
    fake = FakeGoogle({"/token": [503, 503, TOKEN], "/profile": [{"historyId": "1"}]})
    with gmail(fake) as box:
        assert box.profile()[1] == "1"
    assert waits == [2, 4]


def test_a_sign_in_address_busy_too_long_says_busy_not_the_client_id(waits):
    fake = FakeGoogle({"/token": [503], "/profile": [{}]})
    with gmail(fake) as box, pytest.raises(GmailError, match="busy") as error:
        box.profile()
    assert "client ID" not in str(error.value) and waits == [2, 4, 8]


def test_one_wait_budget_is_shared_by_every_request_of_a_pass(waits):
    fake = FakeGoogle({"/profile": [429, 429, 429, {"historyId": "1"}, 429, {"historyId": "2"}]})
    with gmail(fake) as box:
        assert box.profile()[1] == "1"
        with pytest.raises(GmailError, match="busy"):
            box.profile()
    assert waits == [2, 4, 8]


@pytest.mark.parametrize("after, seen", [("5", [5]), ("300", [])])
def test_retry_after_is_used_up_to_a_minute_within_the_budget(waits, after, seen):
    busy = httpx.Response(429, headers={"Retry-After": after})
    fake = FakeGoogle({"/profile": [busy, {"historyId": "1"}]})
    with gmail(fake) as box:
        if seen:
            assert box.profile()[1] == "1"
        else:  # a minute is more than this pass may still wait
            with pytest.raises(GmailError, match="busy"):
                box.profile()
    assert waits == seen


def test_paging_stops_at_the_cap_and_the_change_list_resumes_from_the_last_change_read(monkeypatch):
    monkeypatch.setattr(client, "MAX_PAGES", 3)
    fake = FakeGoogle({
        "/messages": [{"messages": [{"id": "a"}], "nextPageToken": "again"}],
        "/history": [{"history": [{"id": "41", "messagesAdded": [{"message": {"id": "d"}}]}], "historyId": "99",
                      "nextPageToken": "again"}],
    })  # fmt: skip
    with gmail(fake) as box:
        assert box.search("x") == ["a", "a", "a"]
        assert box.history("40") == (["d", "d", "d"], "41")
    assert fake.calls("/messages") == 3 and fake.calls("/history") == 3
