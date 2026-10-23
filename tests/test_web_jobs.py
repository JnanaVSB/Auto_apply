"""The jobs list: filters, sorting, paging and safe display, on a throwaway database with made-up jobs."""

import time
from contextlib import closing
from dataclasses import replace
from urllib.parse import urlencode

import pytest
import sqlcipher3
from helpers import add_job, csrf, log_in

from app import db
from app.web import queries


@pytest.fixture
def four(conn):
    """Four jobs that differ in every filter."""
    with conn:
        add_job(conn, "Alpha", remote="remote", sponsorship="offers", role_category="AI/ML/Data",
                terms='["Summer 2027"]', date_posted="2026-10-10")  # fmt: skip
        add_job(conn, "Beta", ["lever:globex"], company="Initech", level="new grad", us_location="unclear",
                check_location=1, remote="hybrid", date_posted="2026-09-01", added_by_you=1)  # fmt: skip
        add_job(conn, "Gamma", ["simplify:Summer2027-Internships"], company="Globex", level="senior",
                us_location="no", remote="on-site", status="no longer available",
                status_reason="removed from its board")  # fmt: skip
        add_job(conn, "Delta", ["manual"], not_interested=1, date_posted="2026-10-05")


def titles(conn, address=""):
    return [job["title"] for job in queries.list_jobs(conn, queries.Filters.from_address(address)).jobs]


def test_first_view_shows_open_jobs_and_counts_the_others(conn, four):
    page = queries.list_jobs(conn, queries.Filters())
    assert ([job["title"] for job in page.jobs], page.total, page.other_statuses) == (["Alpha", "Beta"], 2, 1)


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("f=1", ["Alpha", "Beta", "Gamma"]),  # every status; no date posted goes last
        ("f=1&level=senior", ["Gamma"]),
        ("f=1&level=intern&level=new+grad", ["Alpha", "Beta"]),
        ("f=1&us=no", ["Gamma"]),
        ("f=1&check=1", ["Beta"]),
        ("f=1&remote=hybrid", ["Beta"]),
        ("f=1&sponsorship=offers", ["Alpha"]),
        ("f=1&status=no+longer+available&reason=removed+from+its+board", ["Gamma"]),
        ("f=1&posted_from=2026-10-01", ["Alpha"]),
        ("f=1&posted_to=2026-09-30", ["Beta"]),
        ("f=1&category=AI%2FML%2FData", ["Alpha"]),
        ("f=1&term=Summer+2027", ["Alpha"]),
        ("f=1&added=1", ["Beta"]),
        ("f=1&hidden=only", ["Delta"]),
        ("f=1&hidden=show", ["Alpha", "Delta", "Beta", "Gamma"]),
        ("f=1&source=greenhouse", ["Alpha"]),
        ("f=1&source=lever:globex", ["Beta"]),
        ("f=1&source=simplify", ["Gamma"]),
        ("f=1&source=manual&hidden=show", ["Delta"]),
        ("f=1&source=lever&board=greenhouse:acme", ["Alpha"]),  # a typed board wins over the source list
        ("f=1&q=GLOB", ["Gamma"]),  # company, capital letters ignored
        ("f=1&level=intern&remote=remote&us=yes", ["Alpha"]),
    ],
)
def test_each_filter(conn, four, address, expected):
    assert titles(conn, address) == expected


def test_values_the_dashboard_does_not_know_are_ignored():
    odd = "f=1&level=wizard&posted_from=yesterday&sort=random&page=-3&source=evil.example&hidden=x&status=gone"
    assert queries.Filters.from_address(odd) == queries.Filters(status=())
    assert queries.Filters.from_address("page=²").page == 1  # "²" counts as a digit for Python's isdigit()
    assert queries.Filters.from_address("&".join(["level=intern"] * 300)) == queries.Filters()  # absurd address


@pytest.mark.parametrize(
    ("words", "expected"),
    [("%", ["100% remote"]), ("o'brien", ["O'Brien's ML team"]), ("ingénieur", ["Ingénieur IA"]),
     ("_", []), ("' OR 1=1 --", []), ("<b>", ["<b>Bold</b> & co"])],
)  # fmt: skip
def test_search_treats_every_character_as_text(conn, words, expected):
    with conn:
        for title in ("100% remote", "O'Brien's ML team", "Ingénieur IA", "<b>Bold</b> & co"):
            add_job(conn, title)
    assert titles(conn, urlencode({"q": words})) == expected
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 4  # nothing typed reaches the database as SQL


@pytest.mark.parametrize(
    ("sort", "expected"),
    [("posted", ["b", "C", "a"]), ("found", ["C", "a", "b"]), ("company", ["b", "C", "a"]), ("title", ["a", "b", "C"])],
)
def test_sorting(conn, sort, expected):
    with conn:
        add_job(conn, "a", company="Zeta", first_seen="2026-10-02")
        add_job(conn, "b", company="alpha", first_seen="2026-10-01", date_posted="2026-10-05")
        add_job(conn, "C", company="Mid", first_seen="2026-10-03", date_posted="2026-09-05")
    assert titles(conn, f"sort={sort}") == expected


def test_pages_of_50_and_a_page_past_the_end_shows_the_last(conn):
    with conn:
        for number in range(120):
            add_job(conn, f"Job {number:03}")
    page = queries.list_jobs(conn, queries.Filters.from_address("sort=title&page=99"))
    assert (page.number, page.pages, page.first, len(page.jobs), page.jobs[0]["title"]) == (3, 3, 101, 20, "Job 100")


def test_a_view_survives_its_own_address():
    view = queries.Filters(q="ml", source="greenhouse:acme", level=("intern", "mid"), check=True, status=(), page=2)
    assert queries.Filters.from_address(view.address().removeprefix("/?")) == view
    assert queries.Filters.from_address(view.address(page=3).removeprefix("/?")) == replace(view, page=3)


@pytest.mark.parametrize("hidden", ["hide", "show"])
def test_the_list_reads_only_the_speed_index(conn, hidden):
    view = queries.Filters(q="x", source="greenhouse", level=("intern",), us=("yes",), check=True, remote=("remote",),
                           posted_from="2026-01-01", category="c", term="t", sponsorship=("offers",), reason="r",
                           added=True, hidden=hidden)  # fmt: skip
    where, values = queries._where(view)
    sql = f"EXPLAIN QUERY PLAN SELECT {queries.LIST_COLUMNS} FROM jobs WHERE {where}"  # noqa: S608 (fixed rules)
    plan = str(conn.execute(sql, values).fetchall())
    assert "COVERING INDEX jobs_list" in plan and "PRIMARY KEY" not in plan  # never a lookup in the jobs table


def test_the_page_shows_job_text_as_plain_text(client, conn, four):
    with conn:
        add_job(conn, "<script>alert('job')</script>", company='Evil "Co" <img src=x onerror=alert(1)>')
    log_in(client)
    text = client.get("/").text
    assert "&lt;script&gt;alert(&#39;job&#39;)&lt;/script&gt;" in text and "<script>alert" not in text
    assert "&lt;img src=x onerror=alert(1)&gt;" in text
    assert "Showing 1–3 of 3 jobs." in text and "+1 with another status" in text
    assert "<option selected>AI/ML/Data</option>" not in text and "<option>AI/ML/Data</option>" in text
    assert "Simplify Summer2027-Internships" not in text  # no Simplify list was ever read in this database


def test_pages_keep_working_while_a_collection_saves(client, conn, data_dir):
    with conn:
        for number in range(3000):
            add_job(conn, f"Job {number}")
    log_in(client)
    with closing(db.connect(data_dir)) as saving:  # a collection in the middle of saving a big list
        saving.execute("PRAGMA cache_size = 10")  # a tiny memory cache, so the save spills to disk at once
        saving.execute("BEGIN IMMEDIATE")
        saving.execute("UPDATE jobs SET last_seen = 'now'")
        started = time.monotonic()
        assert "3,000 jobs" in client.get("/").text  # the list still loads, at once
        assert client.get("/jobs/3000").status_code == 200
        assert time.monotonic() - started < 1
        started = time.monotonic()
        response = client.post("/jobs/1/not-interested", data={"value": "1", "csrf_token": csrf(client)})
        assert (response.status_code, "The database is busy" in response.text) == (503, True)  # changes wait
        assert time.monotonic() - started < 3
        saving.rollback()


def test_a_busy_database_says_try_again(client, monkeypatch):
    def locked(*args):
        raise sqlcipher3.OperationalError("database is locked")

    log_in(client)
    monkeypatch.setattr(queries, "list_jobs", locked)
    response = client.get("/")
    assert (response.status_code, "The database is busy" in response.text) == (503, True)
