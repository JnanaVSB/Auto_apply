"""The phone-book refresh, with a made-up Common Crawl index, a fake web and a throwaway database."""

import json
from contextlib import closing

import httpx
import pytest
from helpers import FakeWeb, ashby_job, board_job, one, settings

from app import collect, fetch, jobs, phonebook
from app.sources import BOARD_HOSTS

pytestmark = pytest.mark.usefixtures("no_waiting")  # downloads never really wait

CRAWL = "CC-MAIN-2026-40"
INDEX = f"https://index.commoncrawl.org/{CRAWL}-index?url={{}}/*&output=json&fl=url"
LEVER_ACME = "https://api.lever.co/v0/postings/acme?mode=json"
LEVER_GONE = "https://api.lever.co/v0/postings/gone?mode=json"
GREENHOUSE_ACME = "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
ASHBY_ACME = "https://api.ashbyhq.com/posting-api/job-board/acme?includeCompensation=true"
TITLES = ["ML Engineer", "Data Scientist", "Research Intern"]


def lever(title, number):
    return {"id": f"l-{number}", "text": title, "hostedUrl": f"https://jobs.lever.co/acme/l-{number}",
            "categories": {"location": "Austin, TX"}}  # fmt: skip


def greenhouse(title, number):
    return board_job(id=number, title=title, absolute_url=f"https://job-boards.greenhouse.io/acme/jobs/{number}")


def index_page(*urls, extra=""):
    """One page of the index: a JSON line per web address."""
    return httpx.Response(200, text="".join(json.dumps({"url": url}) + "\n" for url in urls) + extra)


def crawl_pages(lever_urls=(), greenhouse_urls=()):
    """The made-up index: one crawl, one page per job-board site."""
    pages = {phonebook.CRAWLS: [{"id": CRAWL, "name": "October 2026"}, {"id": "not a crawl"}]}
    for host in BOARD_HOSTS:
        urls = lever_urls if host == "jobs.lever.co" else greenhouse_urls if host == "job-boards.greenhouse.io" else ()
        pages[INDEX.format(host) + "&showNumPages=true"] = {"pages": 1, "pageSize": 5, "blocks": 1}
        pages[INDEX.format(host) + "&page=0"] = index_page(*urls)
    return pages


LEVER_URLS = ("https://jobs.lever.co/acme/l-1", "https://jobs.lever.co/ACME/l-2/apply", "https://jobs.lever.co/gone")
BOARDS_PAGES = {LEVER_ACME: [lever(title, n) for n, title in enumerate(TITLES)], LEVER_GONE: 404}


@pytest.fixture(autouse=True)
def one_crawl(monkeypatch):
    monkeypatch.setattr(phonebook, "CRAWLS_TO_READ", 1)


def refresh(conn, data_dir, pages, **web_options):
    """One refresh against a fake web; returns the printed lines and the web."""
    web, lines = FakeWeb(pages, **web_options), []
    with closing(fetch.Fetcher(httpx.MockTransport(web))) as fetcher:
        phonebook.refresh(conn, jobs.Roles.from_settings(settings(data_dir).roles), fetcher, lines.append)
    return lines, web


def results(conn):
    return dict(conn.execute("SELECT board, result FROM phonebook").fetchall())


def test_a_refresh_finds_checks_and_adds_boards_and_their_jobs(conn, data_dir):
    same_jobs = {"jobs": [greenhouse(title, n) for n, title in enumerate(TITLES)]}  # the same company
    lines, _ = refresh(conn, data_dir, crawl_pages(LEVER_URLS) | BOARDS_PAGES | {GREENHOUSE_ACME: same_jobs})
    assert results(conn) == {
        "lever:acme": "added", "lever:gone": "not found", "greenhouse:acme": "added", "ashby:acme": "not found"
    }  # fmt: skip
    sources = conn.execute("SELECT name, found_by, company FROM sources ORDER BY name").fetchall()
    assert sources == [("greenhouse:acme", "cross-check", "Acme"), ("lever:acme", "commoncrawl", None)]
    assert one(conn, "SELECT count(*) FROM job_sources WHERE source = 'lever:acme'") == (1,)  # ML Engineer matches
    assert lines[-1] == (
        "Checked 4 names: 2 added (read on every collection from now on), 2 not found, 0 to confirm on the Boards "
        "page, 0 could not be checked (asked again next time)."
    )  # a name that was not found is not tried on the other sites
    assert one(conn, "SELECT outcome FROM phonebook_runs") == ("done",)


def test_a_cross_check_board_with_other_jobs_waits_for_you(conn, data_dir):
    other = {"jobs": [greenhouse("Accountant", 1)]}  # maybe another company with the same short name
    refresh(conn, data_dir, crawl_pages(LEVER_URLS) | BOARDS_PAGES | {GREENHOUSE_ACME: other, ASHBY_ACME: 404})
    assert results(conn)["greenhouse:acme"] == "to confirm"
    assert one(conn, "SELECT company FROM phonebook WHERE board = 'greenhouse:acme'") == ("Acme",)
    assert one(conn, "SELECT count(*) FROM sources WHERE name = 'greenhouse:acme'") == (0,)  # not read until you say


def test_running_again_repeats_nothing(conn, data_dir):
    pages = crawl_pages(LEVER_URLS) | BOARDS_PAGES
    refresh(conn, data_dir, pages)
    before = conn.execute("SELECT * FROM phonebook ORDER BY board").fetchall()
    lines, web = refresh(conn, data_dir, pages)
    assert [str(r.url) for r in web.requests if r.method == "GET"] == [phonebook.CRAWLS]  # every page is done
    assert conn.execute("SELECT * FROM phonebook ORDER BY board").fetchall() == before
    assert lines[0] == "Common Crawl: 0 new names"


def test_known_boards_are_not_checked_again(conn, data_dir):
    with conn:
        conn.execute("INSERT INTO sources (name, found_by) VALUES ('lever:acme', 'simplify')")
    refresh(conn, data_dir, crawl_pages(LEVER_URLS) | BOARDS_PAGES)
    assert results(conn) == {"lever:gone": "not found"}
    assert one(conn, "SELECT found_by FROM sources WHERE name = 'lever:acme'") == ("simplify",)


def test_an_interrupted_refresh_continues_where_it_stopped(conn, data_dir):
    pages = crawl_pages(LEVER_URLS) | BOARDS_PAGES | {LEVER_GONE: KeyboardInterrupt()}
    with pytest.raises(KeyboardInterrupt):
        refresh(conn, data_dir, pages)
    assert one(conn, "SELECT outcome FROM phonebook_runs") == ("cancelled",)
    assert set(results(conn)) >= {"lever:acme", "lever:gone"}  # every name found is kept
    _, web = refresh(conn, data_dir, pages | {LEVER_GONE: 404})
    assert not any("page=" in str(request.url) for request in web.requests)  # the index is not read again
    assert results(conn)["lever:gone"] == "not found"


def test_common_crawl_overloaded_keeps_what_was_found_and_still_checks(conn, data_dir, no_waiting):
    with conn:
        values = {"board": "lever:acme", "found_by": "commoncrawl", "on": "2026-10-01", "cross_of": None}
        conn.execute(phonebook.ADD_NAME, values)  # found by an earlier refresh, not checked yet
    lines, _ = refresh(conn, data_dir, BOARDS_PAGES | {phonebook.CRAWLS: 503})
    assert lines[0] == (
        "Common Crawl's index could not be read (busy or failing (status 503); gave up after 3 retries). "
        "Names found so far are kept; try again later."
    )
    assert fetch.COMMON_CRAWL_RETRY_WAITS == (30, 60, 120)
    assert no_waiting.waits[:3] == list(fetch.COMMON_CRAWL_RETRY_WAITS)  # long, patient waits
    assert results(conn)["lever:acme"] == "added"
    assert one(conn, "SELECT outcome FROM phonebook_runs") == ("partial",)  # the schedule tries again sooner


def test_names_a_site_could_not_answer_are_asked_again_next_time(conn, data_dir):
    pages = crawl_pages(LEVER_URLS) | {LEVER_ACME: 500, LEVER_GONE: 404}
    lines, _ = refresh(conn, data_dir, pages)
    assert "1 could not be checked (asked again next time)" in lines[-1]
    assert one(conn, "SELECT checked_on, result FROM phonebook WHERE board = 'lever:acme'") == (None, None)


def test_damaged_index_lines_and_odd_names_are_skipped_and_counted(conn, data_dir):
    pages = crawl_pages() | {
        INDEX.format("jobs.lever.co") + "&page=0": index_page(
            "https://jobs.lever.co/" + "a" * 101,  # far too long
            "https://jobs.lever.co/..%2Fadmin",
            "https://jobs.lever.co/",
            extra='{"url": 5}\nnot json\n',
        )
    }
    lines, _ = refresh(conn, data_dir, pages)
    assert "Common Crawl: 0 new names (2 damaged index lines skipped)" in lines
    assert results(conn) == {}


def test_a_changed_crawl_list_is_reported(conn, data_dir):
    lines, _ = refresh(conn, data_dir, {phonebook.CRAWLS: {"crawls": []}})
    assert lines[0].startswith("Common Crawl's index could not be read (its list of crawls looks different")


def test_no_internet_stops_at_once(conn, data_dir):
    with pytest.raises(collect.CollectError, match="No internet connection"):
        refresh(conn, data_dir, {}, down=fetch.ALLOWED_HOSTS)
    assert one(conn, "SELECT count(*) FROM phonebook_runs") == (0,)


def test_a_refresh_waits_for_a_collection_and_a_second_refresh(data_dir, monkeypatch, capsys):
    monkeypatch.setattr(phonebook, "load_config", lambda: settings(data_dir))
    with closing(collect.lock(data_dir)):
        assert phonebook.main(["refresh"]) == 1
    assert capsys.readouterr().err == f"Error: {collect.BUSY}\n"


def test_a_collection_never_contacts_common_crawl(conn, data_dir):
    web = FakeWeb({})
    with closing(fetch.Fetcher(httpx.MockTransport(web))) as fetcher:
        collect.collect(conn, settings(data_dir), fetcher, [].append)
    assert web.requests
    assert all(request.url.host != fetch.COMMON_CRAWL for request in web.requests)


def test_the_cross_check_compares_job_titles(conn, data_dir):
    lever_jobs = [lever(title, n) for n, title in enumerate(TITLES[:2])]  # only 2 titles in common: not enough
    ashby = {"jobs": [ashby_job(id=f"a-{n}", title=title) for n, title in enumerate(TITLES)]}
    refresh(conn, data_dir, crawl_pages(LEVER_URLS) | {LEVER_ACME: lever_jobs, LEVER_GONE: 404, ASHBY_ACME: ashby})
    assert results(conn)["ashby:acme"] == "to confirm"
