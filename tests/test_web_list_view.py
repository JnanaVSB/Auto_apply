"""The Table | Cards switch on the job lists: kept in this browser's cookie and in the page address, refused without
the safety token, and always back to a list page."""

import pytest
from helpers import add_job, csrf, log_in

from app.web import main


@pytest.fixture
def client(client, conn):
    with conn:
        add_job(conn, "ML Intern")
    log_in(client)
    return client


def switch(client, view, back="/"):
    form = {"csrf_token": csrf(client), "view": view, "back": back}
    return client.post("/view", data=form, follow_redirects=False)


def test_the_switch_keeps_the_view_and_returns_to_the_same_list(client):
    reply = switch(client, "cards", main.LOW_SCORE + "&page=2")
    assert reply.status_code == 303 and reply.headers["location"] == main.LOW_SCORE + "&page=2&view=cards"
    cookie = reply.headers["set-cookie"]
    assert cookie.startswith(f"{main.VIEW_COOKIE}=cards;") and "Max-Age=31536000" in cookie and "Path=/" in cookie
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Secure" not in cookie
    assert '<div class="list-cards">' in client.get("/").text  # the cookie, without the address
    assert 'value="cards" class="to-cards" aria-pressed="true"' in client.get("/ready").text
    assert switch(client, "table", "/ready?page=3").headers["location"] == "/ready?page=3&view=table"


def test_choosing_the_same_view_again_changes_nothing(client):
    first, second = switch(client, "table"), switch(client, "table")
    assert first.headers["location"] == second.headers["location"] == "/?view=table"
    assert first.headers["set-cookie"] == second.headers["set-cookie"]


def test_without_a_choice_the_stylesheet_decides(client):
    text = client.get("/").text
    assert (
        '<div class="list-auto">' in text and 'class="view-switch auto"' in text and 'aria-pressed="true"' not in text
    )


@pytest.mark.parametrize("value", ["", "grid", "TABLE", "cards2", "table cards"])
def test_an_unknown_or_damaged_view_counts_as_no_choice(client, value):
    client.cookies.set(main.VIEW_COOKIE, value)
    assert '<div class="list-auto">' in client.get(f"/?view={value}").text


def test_the_page_address_shows_a_view_without_any_cookie(client):
    assert '<div class="list-cards">' in client.get("/?view=cards").text
    client.cookies.set(main.VIEW_COOKIE, "cards")
    assert '<div class="list-table">' in client.get("/?view=table").text  # the address wins for that page


@pytest.mark.parametrize(
    "back",
    ["https://evil.example/", "//evil.example/", "/\\evil.example", "/settings", "/jobs/1", "/readyx", "",
     "javascript:alert(1)", "/?q=" + "x" * 5000],
)  # fmt: skip
def test_the_way_back_is_always_a_list_page(client, back):
    assert switch(client, "cards", back).headers["location"] == "/?view=cards"


def test_the_switch_needs_the_safety_token_and_a_known_view(client):
    reply = client.post("/view", data={"view": "cards", "back": "/"}, follow_redirects=False)
    assert reply.status_code == 403 and "set-cookie" not in reply.headers
    reply = switch(client, "grid")
    assert reply.status_code == 400 and "set-cookie" not in reply.headers


def test_the_filters_fold_is_closed_and_names_the_filters_in_use(client):
    assert '<details class="filters-fold">\n<summary>Filters</summary>' in client.get("/").text
    text = client.get(main.LOW_SCORE).text
    assert '<details class="filters-fold">' in text and '<details class="filters-fold" open' not in text
    assert (
        '<summary>Filters <span class="active">· low score, low score (quick check), Sort: highest score</span>' in text
    )


def test_the_fold_names_four_filters_and_counts_the_rest(client):
    text = client.get("/?f=1&status=open&level=intern&level=mid&us=yes&remote=remote&odd=1&sort=title").text
    assert '<span class="active">· intern, mid, US, remote, +2 more</span>' in text
