"""The Boards page: every company board, the boards waiting for you, stop and read again. Made-up data only."""

import re

import pytest
from helpers import add_job, board_job, csrf, log_in, one

from app.config import Config, RoleSettings
from app.sources import greenhouse
from app.web import actions

pytestmark = pytest.mark.usefixtures("no_waiting")  # downloads never really wait

BOARDS = [  # name, found_by, company, last_ok, stopped_on, retired_on
    ("greenhouse:acme", "simplify", "Acme", "2026-10-22T10:00:00+00:00", None, None),
    ("lever:globex", None, None, "2026-10-20T10:00:00+00:00", None, None),
    ("ashby:initech", "commoncrawl", None, None, None, None),
    ("ashby:hooli", "list:someone/jobs", None, None, "2026-10-21", None),
    ("lever:gone", "you", None, None, None, "2026-10-01"),
    ("simplify:Summer2027-Internships", "you", None, None, None, None),
]


@pytest.fixture
def client(client, conn, data_dir, monkeypatch):
    with conn:
        conn.executemany(
            "INSERT INTO sources (name, found_by, company, last_ok, stopped_on, retired_on) VALUES (?, ?, ?, ?, ?, ?)",
            BOARDS,
        )
        add_job(conn, "ML Intern", sources=["greenhouse:acme"])
    roles = RoleSettings(keywords=["ML"])
    monkeypatch.setattr(actions, "load_config", lambda: Config(data_dir=data_dir, roles=roles))
    log_in(client)
    return client


def post(client, address, **form):
    return client.post(address, data=form | {"csrf_token": csrf(client)}, follow_redirects=False)


def rows(text):
    """The board names in the page's table, in order."""
    return re.findall(r'<td><a href="[^"]*" target="_blank" rel="noopener noreferrer">([^<]+)</a></td>', text)


def test_every_company_board_is_listed_with_where_it_came_from(client):
    text = client.get("/boards").text
    assert rows(text) == ["ashby:hooli", "ashby:initech", "greenhouse:acme", "lever:globex", "lever:gone"]
    assert "2 active, then 1 discovered" in text and "1 stopped by you,\n  1 retired" in text
    assert "<td>Common Crawl</td>" in text and "<td>list someone/jobs</td>" in text
    assert "<td>before the phone book</td>" in text  # found before the phone book existed: counted as active
    assert "<td>2026-10-22</td>" in text and "<td>never</td>" in text
    assert "stopped by you on 2026-10-21" in text and "retired on 2026-10-01" in text
    assert 'href="https://job-boards.greenhouse.io/acme" target="_blank" rel="noopener noreferrer"' in text


@pytest.mark.parametrize(
    ("address", "boards"),
    [
        ("/boards?show=discovered", ["ashby:initech"]),
        ("/boards?show=active", ["greenhouse:acme", "lever:globex"]),
        ("/boards?show=stopped", ["ashby:hooli"]),
        ("/boards?q=ACME", ["greenhouse:acme"]),  # the board's name or its company, capitals ignored
        (
            "/boards?show=nonsense&page=²",
            ["ashby:hooli", "ashby:initech", "greenhouse:acme", "lever:globex", "lever:gone"],
        ),  # fmt: skip
        ("/boards?q=%25%27%3Cscript%3E", []),  # quotes, wildcards and tags are plain text
    ],
)
def test_the_boards_can_be_filtered(client, address, boards):
    assert rows(client.get(address).text) == boards


def test_long_lists_are_split_into_pages(client, conn, monkeypatch):
    monkeypatch.setattr("app.web.queries.BOARDS_PAGE_SIZE", 2)
    text = client.get("/boards?page=99").text  # past the end: the last page
    assert rows(text) == ["lever:gone"] and "Page 3 of 3" in text and 'href="/boards?page=2">Previous' in text


def test_stop_and_read_again_return_to_the_same_view(client, conn):
    response = post(client, "/boards/stop", board="lever:globex", value="1", back="/boards?show=active&page=1")
    assert response.headers["location"] == "/boards?show=active&page=1"
    assert one(conn, "SELECT stopped_on IS NOT NULL FROM sources WHERE name = 'lever:globex'") == (1,)
    response = post(client, "/boards/stop", board="lever:globex", value="0", back="https://evil.example/")
    assert response.headers["location"] == "/settings"  # only the dashboard's own addresses
    assert one(conn, "SELECT stopped_on FROM sources WHERE name = 'lever:globex'") == (None,)


@pytest.fixture
def waiting(conn):
    with conn:
        conn.execute(
            "INSERT INTO phonebook (board, found_by, found_on, cross_of, checked_on, result, company) VALUES "
            "('greenhouse:initech', 'cross-check', '2026-10-23', 'ashby:initech', '2026-10-23', 'to confirm', "
            "'Initech Labs')"
        )


@pytest.mark.usefixtures("waiting")
def test_boards_the_cross_check_found_wait_for_you(client):
    text = client.get("/boards").text
    assert "Waiting for you (1)" in text
    assert "the board says the company is <strong>Initech Labs</strong>" in text
    assert "same name as <strong>ashby:initech</strong>" in text


@pytest.mark.usefixtures("waiting")
def test_not_this_company_is_never_offered_again_and_deletes_nothing(client, conn):
    response = post(client, "/boards/decline", board="greenhouse:initech", back="/boards?show=all")
    assert response.headers["location"] == "/boards?show=all"
    assert one(conn, "SELECT result FROM phonebook") == ("declined",)
    assert "Waiting for you" not in client.get("/boards").text
    assert post(client, "/boards/decline", board="greenhouse:initech").status_code == 404  # only once


@pytest.mark.usefixtures("waiting")
def test_adding_a_waiting_board_reads_it_like_add_company(client, conn, web):
    web.pages[greenhouse.api_url("initech")] = {"jobs": [board_job(company_name="Initech Labs")]}
    text = post(client, "/boards/confirm", board="greenhouse:initech").text
    assert "Added: greenhouse:initech (1 jobs on the board, 1 match your roles and are saved)." in text
    assert one(conn, "SELECT result FROM phonebook") == ("added",)
    assert one(conn, "SELECT found_by FROM sources WHERE name = 'greenhouse:initech'") == ("you",)
    assert "Waiting for you" not in text


def test_a_waiting_board_that_is_read_by_now_waits_no_more(client, conn, waiting):
    with conn:
        conn.execute("INSERT INTO sources (name, found_by) VALUES ('greenhouse:initech', 'simplify')")
    assert "Waiting for you" not in client.get("/boards").text
    assert post(client, "/boards/decline", board="greenhouse:initech").status_code == 404  # never declined while read


def test_a_greenhouse_board_without_a_company_name_says_so(client, conn):
    with conn:
        conn.execute(
            "INSERT INTO phonebook (board, found_by, found_on, cross_of, checked_on, result) VALUES "
            "('greenhouse:quiet', 'cross-check', '2026-10-23', 'lever:quiet', '2026-10-23', 'to confirm')"
        )
    text = client.get("/boards").text
    assert "<strong>greenhouse:quiet</strong>:\n      the board does not say its company name" in text
    assert "Greenhouse boards do not report" not in text


def test_boards_with_no_jobs_yet_are_explained(client, conn):
    with conn:
        conn.execute("INSERT INTO phonebook (board, found_by, found_on, checked_on, result) VALUES "
                     "('lever:quiet', 'commoncrawl', '2026-10-23', '2026-10-23', 'empty')")  # fmt: skip
    text = client.get("/boards").text
    assert "1 more boards found by the phone book exist but list no jobs yet" in text
    assert "a shorter refresh interval" in text


def test_the_boards_page_needs_a_login(client):
    client.post("/logout", data={"csrf_token": csrf(client)})
    assert client.get("/boards", follow_redirects=False).headers["location"] == "/login"
