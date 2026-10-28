"""The phone-book refresh, with a made-up Common Crawl index, a fake web and a throwaway database."""

import json
from contextlib import closing
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from helpers import ACME_JOB, LIST, FakeWeb, add_job, ashby_job, board_job, entry, one, settings

from app import collect, fetch, phonebook
from app.config import SimplifyList, SourceSettings
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


NO_LISTS = SourceSettings()


def refresh(conn, data_dir, pages, sources=NO_LISTS, **web_options):
    """One refresh against a fake web, by default with no lists to read; returns the printed lines and the web."""
    web, lines = FakeWeb(pages, **web_options), []
    config = settings(data_dir).model_copy(update={"sources": sources})
    with closing(fetch.Fetcher(httpx.MockTransport(web))) as fetcher:
        phonebook.refresh(conn, config, fetcher, lines.append)
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
        "Checked 4 names: 2 added (read on every collection from now on), 0 with no jobs yet (asked again next "
        "round), 2 not found, 0 to confirm on the Boards page, 0 could not be checked (asked again next time)."
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


OTHER_LIST = "https://raw.githubusercontent.com/someone/jobs/main/README.md"
LISTS = SourceSettings(simplify_lists=[SimplifyList(url=LIST, level="intern")], phonebook_lists=[OTHER_LIST])


def test_lists_give_names_including_simplifys_closed_jobs(conn, data_dir):
    with conn:
        conn.execute("INSERT INTO sources (name, found_by) VALUES ('greenhouse:acme', 'simplify')")  # already read
    closed = entry(id="s-2", url="https://jobs.lever.co/closedco/1", active=False)
    other = httpx.Response(200, text="| Acme | [Apply](https://jobs.ashbyhq.com/MdCo/123) | https://acme.example |")
    pages = crawl_pages() | {LIST: [entry(url=ACME_JOB), closed], OTHER_LIST: other}
    lines, _ = refresh(conn, data_dir, pages, LISTS)
    assert lines[:2] == [
        "Simplify Summer2027-Internships: 2 boards linked, 1 new names",
        "someone/jobs: 1 boards linked, 1 new names",
    ]
    found = conn.execute("SELECT board, found_by FROM phonebook ORDER BY board").fetchall()
    assert found == [("ashby:mdco", "list:someone/jobs"), ("lever:closedco", "simplify")]  # acme is already read
    assert one(conn, "SELECT outcome FROM phonebook_runs") == ("done",)


def test_a_list_that_cannot_be_read_is_named_and_the_rest_goes_on(conn, data_dir):
    pages = crawl_pages(LEVER_URLS) | BOARDS_PAGES | {LIST: [entry()], OTHER_LIST: 404}
    lines, _ = refresh(conn, data_dir, pages, LISTS)
    assert "someone/jobs could not be read (not found (404)); try again later." in lines
    assert results(conn)["lever:acme"] == "added"  # Common Crawl and the checks still ran
    assert one(conn, "SELECT outcome FROM phonebook_runs") == ("partial",)


def test_a_refresh_pauses_before_its_next_request_when_a_collection_waits(conn, data_dir):
    collect.ask_refresh_to_pause(data_dir)
    lines, web = refresh(conn, data_dir, crawl_pages(LEVER_URLS) | BOARDS_PAGES)
    assert lines == [phonebook.PAUSED]
    assert [request.method for request in web.requests] == ["HEAD"]  # only the internet check
    assert one(conn, "SELECT outcome, continues FROM phonebook_runs") == ("paused", 0)
    (data_dir / collect.PAUSE_FILE).unlink()
    refresh(conn, data_dir, crawl_pages(LEVER_URLS) | BOARDS_PAGES)
    assert conn.execute("SELECT outcome, continues FROM phonebook_runs ORDER BY id").fetchall() == [
        ("paused", 0), ("done", 1)]  # fmt: skip


@pytest.mark.parametrize(
    ("last", "every", "continues"),
    [
        (None, 30, False),  # the first refresh starts a round
        (("done", 1), 30, False),  # the last round finished: a new one
        (("partial", 1), 30, True),  # unfinished: continued
        (("paused", 29), 30, True),
        (("partial", 30), 30, False),  # the round began 30 days ago: a new one
        (("partial", 400), 0, True),  # no schedule: an unfinished round is always continued
    ],
)
def test_a_refresh_continues_an_unfinished_round_unless_it_is_too_old(conn, last, every, continues):
    if last:
        with conn:
            started = (datetime.now(UTC) - timedelta(days=last[1])).isoformat()
            conn.execute("INSERT INTO phonebook_runs (started_at, outcome) VALUES (?, ?)", (started, last[0]))
    assert phonebook.continues_round(conn, every, datetime.now(UTC)) is continues


def test_the_command_continues_after_the_collection_it_paused_for(data_dir, monkeypatch, capsys):
    outcomes, waited = iter(["paused", "done"]), []
    monkeypatch.setattr(phonebook, "load_config", lambda: settings(data_dir))
    monkeypatch.setattr(phonebook, "refresh", lambda conn, config, fetcher: next(outcomes))

    def collection_ran(folder):
        waited.append(folder)
        return collect.lock(folder)

    monkeypatch.setattr(collect, "wait_for_collection", collection_ran)
    assert phonebook.main(["refresh"]) == 0
    assert waited == [data_dir]


def done_parts(conn):
    return {part for (part,) in conn.execute("SELECT part FROM phonebook_done")}


def test_not_found_in_the_index_means_no_names_there_and_the_rest_is_read(conn, data_dir):
    pages = (
        crawl_pages(LEVER_URLS)
        | BOARDS_PAGES
        | {
            INDEX.format("boards.greenhouse.io") + "&showNumPages=true": 404,  # no page count
            INDEX.format("jobs.ashbyhq.com") + "&page=0": 404,  # an empty page
        }
    )
    refresh(conn, data_dir, pages)
    assert {f"commoncrawl:{CRAWL}:boards.greenhouse.io", f"commoncrawl:{CRAWL}:jobs.ashbyhq.com"} <= done_parts(conn)
    assert results(conn)["lever:acme"] == "added"
    assert one(conn, "SELECT outcome FROM phonebook_runs") == ("done",)


def test_a_site_whose_index_part_fails_is_skipped_and_asked_again_next_time(conn, data_dir):
    count = INDEX.format("jobs.lever.co") + "&showNumPages=true"
    lines, _ = refresh(conn, data_dir, crawl_pages(LEVER_URLS) | BOARDS_PAGES | {count: 500})
    assert (
        f"Common Crawl {CRAWL}, jobs.lever.co could not be read (busy or failing (status 500); gave up after 3 "
        "retries); tried again next time."
    ) in lines
    assert f"commoncrawl:{CRAWL}:jobs.ashbyhq.com" in done_parts(conn)  # the sites after it were still read
    assert one(conn, "SELECT outcome FROM phonebook_runs") == ("partial",)
    _, web = refresh(conn, data_dir, crawl_pages(LEVER_URLS) | BOARDS_PAGES)
    index = [str(request.url) for request in web.requests if request.url.host == fetch.COMMON_CRAWL]
    assert index == [phonebook.CRAWLS, count, INDEX.format("jobs.lever.co") + "&page=0"]  # only what was missing
    assert results(conn)["lever:acme"] == "added"


def test_an_overloaded_index_is_left_after_two_failed_parts_in_a_row_and_the_names_are_checked(conn, data_dir):
    busy = {INDEX.format(host) + "&showNumPages=true": 504 for host in ("job-boards.greenhouse.io", "jobs.lever.co")}
    pages = (
        crawl_pages(greenhouse_urls=())
        | busy
        | {
            INDEX.format("boards.greenhouse.io") + "&page=0": index_page("https://boards.greenhouse.io/acme/jobs/1"),
            GREENHOUSE_ACME: {"jobs": [greenhouse("ML Engineer", 1)]},
        }
    )
    lines, web = refresh(conn, data_dir, pages)
    failed = [line for line in lines if "could not be read" in line]
    assert failed == [
        f"Common Crawl {CRAWL}, job-boards.greenhouse.io could not be read (busy or failing (status 504); gave up after"
        " 3 retries); tried again next time.",
        f"Common Crawl {CRAWL}, jobs.lever.co could not be read (busy or failing (status 504); gave up after 3"
        " retries); tried again next time.",
    ]  # said as they happen, not at the end
    assert lines[lines.index(failed[1]) + 1] == phonebook.OVERLOADED
    assert INDEX.format("jobs.ashbyhq.com") + "&showNumPages=true" not in [str(r.url) for r in web.requests]
    assert results(conn)["greenhouse:acme"] == "added"  # the names found so far were still checked
    assert one(conn, "SELECT outcome FROM phonebook_runs") == ("partial",)


def test_reading_the_index_says_what_it_is_doing(conn, data_dir):
    host = "jobs.lever.co"
    pages = crawl_pages(LEVER_URLS) | BOARDS_PAGES | {INDEX.format(host) + "&showNumPages=true": {"pages": 12}}
    pages |= {INDEX.format(host) + f"&page={page}": index_page() for page in range(1, 12)}
    lines, _ = refresh(conn, data_dir, pages)
    assert lines[0] == phonebook.READING
    assert f"Common Crawl {CRAWL}, {host}: 12 index pages to read" in lines
    assert "  page 11 of 12" in lines
    assert f"Common Crawl {CRAWL}, boards.greenhouse.io: 1 index page to read" in lines


def test_an_index_that_stops_answering_ends_its_reading_until_next_time(conn, data_dir):
    first = INDEX.format("boards.greenhouse.io") + "&showNumPages=true"
    lines, web = refresh(conn, data_dir, crawl_pages(LEVER_URLS) | {first: httpx.ConnectError("dropped")})
    assert lines[0] == phonebook.READING
    assert lines[1].startswith("Common Crawl's index could not be read (connection failed (ConnectError)")
    assert len([request for request in web.requests if request.url.host == fetch.COMMON_CRAWL]) == 1 + 4  # 3 retries
    assert one(conn, "SELECT outcome FROM phonebook_runs") == ("partial",)


def test_a_page_that_is_not_index_lines_is_asked_again_next_time(conn, data_dir):
    page = INDEX.format("jobs.lever.co") + "&page=0"
    lines, _ = refresh(conn, data_dir, crawl_pages() | {page: httpx.Response(200, text="<html>\nBusy\n</html>\n")})
    assert (
        f"Common Crawl {CRAWL}, jobs.lever.co could not be read (page 1 is not index lines (perhaps an error page)); "
        "tried again next time."
    ) in lines
    assert not [part for part in done_parts(conn) if "jobs.lever.co" in part]
    assert one(conn, "SELECT outcome FROM phonebook_runs") == ("partial",)


def test_a_real_sighting_asks_again_a_name_only_the_cross_check_found_missing(conn, data_dir):
    refresh(conn, data_dir, crawl_pages(LEVER_URLS) | BOARDS_PAGES)
    assert (results(conn)["ashby:acme"], results(conn)["lever:gone"]) == ("not found", "not found")
    gone_checked = one(conn, "SELECT checked_on FROM phonebook WHERE board = 'lever:gone'")
    links = httpx.Response(200, text="[Apply](https://jobs.ashbyhq.com/acme/1) [Old](https://jobs.lever.co/gone/2)")
    ashby = {"jobs": [ashby_job(jobUrl="https://jobs.ashbyhq.com/acme/9f8e-7d6c")]}
    pages = crawl_pages(LEVER_URLS) | {OTHER_LIST: links, ASHBY_ACME: ashby}
    refresh(conn, data_dir, pages, SourceSettings(phonebook_lists=[OTHER_LIST]))
    assert one(conn, "SELECT found_by, result FROM phonebook WHERE board = 'ashby:acme'") == (
        "list:someone/jobs", "added")  # fmt: skip
    assert one(conn, "SELECT checked_on FROM phonebook WHERE board = 'lever:gone'") == gone_checked  # stays decided


def test_a_board_with_no_jobs_is_added_only_once_it_lists_one(conn, data_dir):
    empty = "https://api.lever.co/v0/postings/emptyco?mode=json"
    pages = crawl_pages(("https://jobs.lever.co/emptyco",)) | {empty: []}
    lines, _ = refresh(conn, data_dir, pages)
    assert results(conn) == {"lever:emptyco": "empty"}
    assert one(conn, "SELECT count(*) FROM sources") == (0,)  # not read by collections
    assert "1 with no jobs yet (asked again next round)" in lines[-1]
    with conn:
        conn.execute("UPDATE phonebook_runs SET outcome = 'paused'")
    _, web = refresh(conn, data_dir, pages)  # the same round continues: not asked again
    assert empty not in [str(request.url) for request in web.requests]
    with conn:  # that round finished, and the board was asked before the next round began
        conn.execute("UPDATE phonebook_runs SET outcome = 'done'")
        conn.execute("UPDATE phonebook SET checked_on = '2026-01-01T00:00:00+00:00'")
    hired = [{"id": "e-1", "text": "ML Engineer", "hostedUrl": "https://jobs.lever.co/emptyco/e-1",
              "categories": {"location": "Austin, TX"}}]  # fmt: skip
    refresh(conn, data_dir, pages | {empty: hired})
    assert results(conn)["lever:emptyco"] == "added"
    assert one(conn, "SELECT count(*) FROM job_sources WHERE source = 'lever:emptyco'") == (1,)


def test_the_cross_check_after_a_stop_compares_with_the_jobs_saved_from_the_first_board(conn, data_dir):
    titles = ("ML Engineer", "ML Researcher", "ML Intern")
    with conn:
        conn.execute("INSERT INTO sources (name, found_by) VALUES ('lever:acme', 'commoncrawl')")
        for title in titles:
            add_job(conn, title, sources=["lever:acme"])
        values = {"board": "greenhouse:acme", "found_by": "cross-check", "on": "2026-10-01", "cross_of": "lever:acme"}
        conn.execute(phonebook.ADD_NAME, values)  # left unchecked by a refresh that was stopped
    same = {"jobs": [greenhouse(title, number) for number, title in enumerate(titles)]}
    refresh(conn, data_dir, crawl_pages() | {GREENHOUSE_ACME: same})
    assert results(conn)["greenhouse:acme"] == "added"


def test_the_cross_check_compares_job_titles(conn, data_dir):
    lever_jobs = [lever(title, n) for n, title in enumerate(TITLES[:2])]  # only 2 titles in common: not enough
    ashby = {"jobs": [ashby_job(id=f"a-{n}", title=title) for n, title in enumerate(TITLES)]}
    refresh(conn, data_dir, crawl_pages(LEVER_URLS) | {LEVER_ACME: lever_jobs, LEVER_GONE: 404, ASHBY_ACME: ashby})
    assert results(conn)["ashby:acme"] == "to confirm"
