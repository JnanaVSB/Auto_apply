"""USAJobs' search, with made-up replies. Nobody here has had a key: their shape comes from its documentation only."""

import json
from contextlib import closing
from urllib.parse import urlencode

import httpx
import pytest
from helpers import FakeWeb, one, settings

from app import collect, fetch, jobs, keychain
from app.config import SourceSettings
from app.sources import Skipped, SourceError, remoteok, usajobs
from app.sources.feeds import FEEDS
from app.web import queries

pytestmark = pytest.mark.usefixtures("no_waiting")  # downloads never really wait

KEY, MAIL = "fake-key-1234", "sam.fake@example.com"
LINK = "https://www.usajobs.gov/GetJob/ViewDetails/123456"
FEED = "feed:usajobs"
LEVER = "https://api.lever.co/v0/postings/acme?mode=json"


def descriptor(**changes):
    entry = {
        "PositionTitle": "ML Engineer",
        "OrganizationName": "Department of Examples",
        "PositionURI": "https://www.usajobs.gov:443/GetJob/ViewDetails/123456",
        "PositionLocation": [{"LocationName": "Austin, Texas", "CountryCode": "United States"}],
        "PublicationStartDate": "2026-10-19T00:00:00.0000",
        "PositionRemuneration": [{"MinimumRange": "85000.0", "MaximumRange": "120000.0", "RateIntervalCode": "PA",
                                  "Description": "Per Year"}],
        "UserArea": {"Details": {"JobSummary": "<p>Train models.</p>", "MajorDuties": ["Build.", "Ship."],
                                 "WhoMayApply": {"Name": "United States Citizens", "Code": "15514"}}},
    }  # fmt: skip
    return entry | changes


def details(**changes):
    return {"UserArea": {"Details": changes}}


def item(**changes):
    return {"MatchedObjectId": "123456", "MatchedObjectDescriptor": descriptor(**changes)}


def numbered(number):
    return item(PositionURI=f"https://www.usajobs.gov/GetJob/ViewDetails/{number}")


def reply(*items, pages="1"):
    return {"SearchResult": {"SearchResultCount": len(items), "SearchResultItems": list(items),
                             "UserArea": {"NumberOfPages": pages}}}  # fmt: skip


def search(word="ML", page=1):
    return f"{usajobs.API}?{urlencode({'Keyword': word, 'ResultsPerPage': 500, 'Page': page})}"


@pytest.fixture
def key():
    keychain.set_secret(usajobs.KEY, KEY)
    keychain.set_secret(usajobs.EMAIL, MAIL)


def read(pages, keywords=("ML",)):
    web = FakeWeb(pages)
    with closing(fetch.Fetcher(httpx.MockTransport(web))) as fetcher:
        return usajobs.read(fetcher, list(keywords)), web


def one_job(**changes):
    (found, skipped, _), _ = read({search(): reply(item(**changes))})
    assert skipped == 0
    return found[0]


def run(conn, data_dir, pages, feeds=("usajobs",), extra_boards=(), keywords=None):
    web, lines = FakeWeb(pages), []
    config = settings(data_dir).model_copy(
        update={"sources": SourceSettings(feeds=list(feeds), extra_boards=list(extra_boards))}
    )
    if keywords is not None:
        config = config.model_copy(update={"roles": config.roles.model_copy(update={"keywords": keywords})})
    with closing(fetch.Fetcher(httpx.MockTransport(web))) as fetcher:
        collect.collect(conn, config, fetcher, lines.append)
    return lines, web


def hosts(web):
    return {request.url.host for request in web.requests if request.method == "GET"}


@pytest.mark.usefixtures("key")
def test_a_listing_becomes_a_job():
    job = one_job()
    assert (job.source, job.source_job_id, job.url) == (FEED, LINK, LINK)
    assert (job.company, job.title, job.locations) == ("Department of Examples", "ML Engineer", ["Austin, Texas"])
    assert job.place_checks == [("Austin, Texas", "United States")]
    assert job.salary == "USD 85,000–120,000 per year"
    assert job.date_posted == "2026-10-19"
    assert job.description == "Train models.\n\nBuild.\nShip."
    assert job.sponsorship == "citizens only"


@pytest.mark.usefixtures("key")
def test_each_request_carries_the_key_and_e_mail_in_its_headers_only():
    _, web = read({search(): reply(item())})
    request = web.requests[0]
    assert (request.headers["Authorization-Key"], request.headers["User-Agent"]) == (KEY, MAIL)
    assert KEY not in str(request.url) and MAIL not in str(request.url)


@pytest.mark.parametrize(
    ("secrets", "keywords", "line"),
    [
        ({}, ["ML"], usajobs.NEEDS_KEY),
        ({"usajobs-key": KEY}, ["ML"], usajobs.NEEDS_KEY),
        ({"usajobs-email": MAIL}, ["ML"], usajobs.NEEDS_KEY),
        ({"usajobs-key": KEY, "usajobs-email": MAIL}, [], usajobs.NO_KEYWORDS),
        ({"usajobs-key": KEY, "usajobs-email": MAIL}, ["  "], usajobs.NO_KEYWORDS),
    ],
)
def test_with_nothing_to_ask_with_it_says_so_and_asks_nothing(secrets, keywords, line):
    for name, value in secrets.items():
        keychain.set_secret(name, value)
    web = FakeWeb({})
    with closing(fetch.Fetcher(httpx.MockTransport(web))) as fetcher, pytest.raises(Skipped, match=line):
        usajobs.read(fetcher, keywords)
    assert web.requests == []


def test_a_keychain_that_fails_is_said_and_asks_nothing(monkeypatch):
    def locked(_):
        raise keychain.KeychainError("No secure OS keychain available (found: none).")

    monkeypatch.setattr(usajobs, "get_secret", locked)
    with pytest.raises(Skipped, match="No secure OS keychain"):
        read({})


def test_skipping_is_one_line_closes_nothing_and_is_no_failure(conn, data_dir):
    lines, web = run(conn, data_dir, {})
    assert f"USAJobs: {usajobs.NEEDS_KEY}" in lines
    assert "No sources failed." in lines
    assert not hosts(web) & fetch.FEED_HOSTS


@pytest.mark.usefixtures("key")
def test_a_job_found_by_two_keywords_is_one_job():
    (found, _, complete), web = read({search("ML"): reply(item()), search("data"): reply(item())}, ["ML", "data"])
    assert len(found) == 1 and complete
    assert len(web.requests) == 2


@pytest.mark.usefixtures("key")
def test_a_keyword_with_spaces_and_an_ampersand_is_encoded():
    _, web = read({search("R&D lead"): reply(item())}, ["R&D lead"])
    assert "Keyword=R%26D+lead" in str(web.requests[0].url)


@pytest.mark.usefixtures("key")
def test_a_failed_page_saves_and_closes_nothing(conn, data_dir):
    run(conn, data_dir, {search(): reply(item())})
    before = conn.execute("SELECT * FROM job_sources").fetchall()
    pages = {search(): reply(numbered(2), pages="2"), search(page=2): 500}
    lines, _ = run(conn, data_dir, pages)
    assert any(line.startswith("  - USAJobs: busy or failing (status 500)") for line in lines)
    assert conn.execute("SELECT * FROM job_sources").fetchall() == before
    assert one(conn, "SELECT status FROM jobs") == ("open",)


@pytest.mark.usefixtures("key")
def test_paging_stops_where_the_reply_says():
    pages = {search(): reply(numbered(1), pages="2"), search(page=2): reply(numbered(2), pages="2")}
    (found, _, complete), web = read(pages)
    assert len(found) == 2 and complete
    assert len(web.requests) == 2


@pytest.mark.usefixtures("key")
def test_at_the_page_cap_jobs_are_saved_but_none_closed(conn, data_dir):
    run(conn, data_dir, {search(): reply(numbered(99))})
    pages = {search(page=page): reply(numbered(page), pages="9") for page in range(1, 10)}
    (found, _, complete), web = read(pages)
    assert (len(found), complete, len(web.requests)) == (usajobs.MAX_PAGES, False, usajobs.MAX_PAGES)
    run(conn, data_dir, pages)
    assert conn.execute("SELECT status FROM jobs WHERE apply_url LIKE '%/99'").fetchall() == [("open",)]


@pytest.mark.usefixtures("key")
@pytest.mark.parametrize(
    ("pages", "complete"), [("text", False), (True, False), (None, False), ("0", True), (0, True), (1, True)]
)
def test_an_odd_page_count_never_counts_as_everything_read(pages, complete):
    (_, _, whole), _ = read({search(): reply(item(), pages=pages)})
    assert whole is complete


@pytest.mark.usefixtures("key")
@pytest.mark.parametrize("area", ["text", [], ["x"], None, 5])
def test_an_odd_user_area_means_how_far_it_goes_is_unknown(area):
    page = reply(item())
    page["SearchResult"]["UserArea"] = area
    (found, _, whole), _ = read({search(): page})
    assert (len(found), whole) == (1, False)


@pytest.mark.parametrize(
    ("key", "mail"),
    [("clé-1234", MAIL), (KEY, "sám@example.com"), ("line\nbreak", MAIL), (KEY, "sam\0@example.com"), ("a b", MAIL)],
)
def test_a_saved_value_that_cannot_be_sent_is_said_and_asks_nothing(conn, data_dir, key, mail):
    keychain.set_secret(usajobs.KEY, key)
    keychain.set_secret(usajobs.EMAIL, mail)
    lines, web = run(conn, data_dir, {})
    assert f"USAJobs: {usajobs.CANT_SEND}" in lines
    assert "No sources failed." in lines
    assert not hosts(web) & fetch.FEED_HOSTS
    assert not [line for line in lines if key in line or mail in line]


@pytest.mark.usefixtures("key")
@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_key_says_what_to_check(conn, data_dir, status):
    lines, _ = run(conn, data_dir, {search(): status})
    assert f"  - USAJobs: {usajobs.KEY_REFUSED}" in lines


@pytest.mark.usefixtures("key")
@pytest.mark.parametrize(
    "bad",
    [{}, {"SearchResult": None}, {"SearchResult": {"SearchResultItems": "x"}}, {"SearchResult": {}}, [], None],
)
def test_a_reply_of_another_shape_is_refused(bad):
    with pytest.raises(SourceError):
        read({search(): httpx.Response(200, text=json.dumps(bad))})


@pytest.mark.usefixtures("key")
def test_an_entry_with_no_descriptor_is_broken():
    (found, skipped, _), _ = read({search(): reply({"MatchedObjectId": "1"}, item())})
    assert (len(found), skipped) == (1, 1)


@pytest.mark.usefixtures("key")
def test_every_entry_broken_is_refused():
    with pytest.raises(SourceError, match="none of its 1 entries"):
        read({search(): reply({"MatchedObjectId": "1"})})


@pytest.mark.usefixtures("key")
@pytest.mark.parametrize(
    "link",
    [
        "javascript:alert(1)",
        "http://www.usajobs.gov/GetJob/ViewDetails/1",
        "https://www.usajobs.gov.evil.com/x",
        "https://evil.com/www.usajobs.gov",
        "https://user@www.usajobs.gov/x",
        "https://www.usajobs.gov:8443/x",
        "https://www.usajobs.gov/",
        "https://[broken/x",
        5,
    ],
)
def test_a_link_that_is_not_a_usajobs_listing_is_a_broken_entry(link):
    (found, skipped, _), _ = read({search(): reply(item(PositionURI=link), item())})
    assert ([job.source_job_id for job in found], skipped) == ([LINK], 1)


@pytest.mark.usefixtures("key")
def test_the_link_is_written_one_way():
    assert one_job(PositionURI="https://WWW.USAJOBS.GOV:443/GetJob/ViewDetails/123456?x=1").source_job_id == LINK


@pytest.mark.usefixtures("key")
@pytest.mark.parametrize(
    ("who", "sponsorship"),
    [
        ({"Name": "United States Citizens", "Code": "15514"}, "citizens only"),
        ("United States Citizens", "citizens only"),
        ({"Name": "Open to the public"}, "unknown"),
        ("Open to the public", "unknown"),
        (["United States Citizens"], "unknown"),
        (5, "unknown"),
        (None, "unknown"),
    ],
)
def test_who_may_apply(who, sponsorship):
    assert one_job(**details(WhoMayApply=who)).sponsorship == sponsorship


@pytest.mark.usefixtures("key")
@pytest.mark.parametrize(
    ("pay", "shown"),
    [
        (
            {"MinimumRange": "85000.0", "MaximumRange": "120000.0", "RateIntervalCode": "PA"},
            "USD 85,000–120,000 per year",
        ),
        (
            {"MinimumRange": "85,000", "MaximumRange": "120,000", "Description": "Per Year"},
            "USD 85,000–120,000 per year",
        ),
        ({"MinimumRange": "30", "MaximumRange": "36", "RateIntervalCode": "Per Hour"}, None),
        ({"MinimumRange": "85000", "MaximumRange": "120000", "RateIntervalCode": "Per Hour"}, None),
        ({"MinimumRange": "abc", "MaximumRange": "120000", "RateIntervalCode": "PA"}, None),
        ({"MinimumRange": None, "MaximumRange": "120000", "RateIntervalCode": "PA"}, None),
        ({"MinimumRange": "120000", "MaximumRange": "85000", "RateIntervalCode": "PA"}, None),
        ({"RateIntervalCode": "PA"}, None),
    ],
)
def test_pay_is_shown_only_for_a_whole_yearly_range(pay, shown):
    assert one_job(PositionRemuneration=[pay]).salary == shown


@pytest.mark.usefixtures("key")
@pytest.mark.parametrize("high", ["inf", "Infinity", "1e999", "nan", "20000000"])
def test_pay_past_any_real_salary_gives_none(high):
    pay = {"MinimumRange": "85000", "MaximumRange": high, "RateIntervalCode": "PA"}
    assert one_job(PositionRemuneration=[pay]).salary is None


@pytest.mark.usefixtures("key")
@pytest.mark.parametrize("name", ["PositionTitle", "OrganizationName"])
def test_a_name_blank_once_cleaned_is_a_broken_entry(name):
    (found, skipped, _), _ = read({search(): reply(item(**{name: "&#32;"}), item())})
    assert (len(found), skipped) == (1, 1)


@pytest.mark.usefixtures("key")
def test_a_search_cut_short_says_why_nothing_was_closed(conn, data_dir):
    pages = {search(page=page): reply(numbered(page), pages="9") for page in range(1, 10)}
    lines, _ = run(conn, data_dir, pages)
    assert [line for line in lines if line.startswith("USAJobs: ")] == [
        "USAJobs: 5 matching (5 new, 0 updated, 0 no longer available), the list was cut short, so nothing was closed"
    ]


@pytest.mark.usefixtures("key")
def test_a_search_cut_short_with_a_broken_entry_says_both(conn, data_dir):
    first = reply(numbered(1), {"MatchedObjectId": "1"}, pages="9")
    pages = {search(page=page): reply(numbered(page), pages="9") for page in range(2, 10)} | {search(): first}
    lines, _ = run(conn, data_dir, pages)
    assert [line for line in lines if line.startswith("USAJobs: ")] == [
        "USAJobs: 5 matching (5 new, 0 updated, 0 no longer available), 1 unusable entry skipped, the list was cut "
        "short, so nothing was closed"
    ]


@pytest.mark.usefixtures("key")
def test_a_broken_entry_in_a_whole_search_closes_nothing(conn, data_dir):
    run(conn, data_dir, {search(): reply(numbered(1), numbered(2))})
    run(conn, data_dir, {search(): reply(numbered(2), {"MatchedObjectId": "1"})})
    assert conn.execute("SELECT DISTINCT status FROM jobs").fetchall() == [("open",)]


@pytest.mark.usefixtures("key")
@pytest.mark.parametrize("pay", [[], None, "x", ["x"]])
def test_no_pay_given_is_no_pay(pay):
    assert one_job(PositionRemuneration=pay).salary is None


@pytest.mark.usefixtures("key")
def test_a_place_abroad_is_judged_by_its_country():
    job = one_job(PositionLocation=[{"LocationName": "Stuttgart", "CountryCode": "Germany"}])
    assert jobs.us_location(job) == "no"


@pytest.mark.usefixtures("key")
def test_many_places_and_a_blank_one():
    places = [{"LocationName": "Reno, Nevada", "CountryCode": "United States"}, {"LocationName": "  "}, "x"]
    job = one_job(PositionLocation=[*places, {"LocationName": "Bonn", "CountryCode": "Germany"}])
    assert job.locations == ["Reno, Nevada", "Bonn"]
    assert jobs.us_location(job) == "yes"


@pytest.mark.usefixtures("key")
def test_no_location_is_kept_as_unclear():
    assert jobs.us_location(one_job(PositionLocation=[])) == "unclear"


@pytest.mark.usefixtures("key")
@pytest.mark.parametrize(
    ("summary", "text"),
    [
        ("<p>Hi</p><script>alert(1)</script>", "Hi"),
        ("&lt;p&gt;Hi&lt;/p&gt;&lt;script&gt;alert(1)&lt;/script&gt;", "Hi"),
        ("R&amp;D", "R&D"),
        ("", None),
    ],
)
def test_details_are_plain_text(summary, text):
    assert one_job(**details(JobSummary=summary)).description == text


@pytest.mark.usefixtures("key")
def test_escaped_names_arrive_as_people_read_them():
    job = one_job(PositionTitle="R&amp;D Engineer", OrganizationName="A &amp; B")
    assert (job.title, job.company) == ("R&D Engineer", "A & B")


@pytest.mark.usefixtures("key")
def test_no_results_with_nothing_listed_is_fine(conn, data_dir):
    lines, _ = run(conn, data_dir, {search(): reply()})
    assert "No sources failed." in lines


@pytest.mark.usefixtures("key")
def test_no_results_with_jobs_listed_closes_nothing(conn, data_dir):
    run(conn, data_dir, {search(): reply(item())})
    lines, _ = run(conn, data_dir, {search(): reply()})
    assert any(line.startswith("Needs investigation") for line in lines)
    assert one(conn, "SELECT status FROM jobs") == ("open",)


@pytest.mark.usefixtures("key")
def test_a_job_gone_from_a_complete_search_closes(conn, data_dir):
    run(conn, data_dir, {search(): reply(numbered(1), numbered(2))})
    run(conn, data_dir, {search(): reply(numbered(2))})
    assert conn.execute("SELECT status_reason FROM jobs ORDER BY id").fetchall() == [(jobs.NOT_IN_FEED,), (None,)]


@pytest.mark.usefixtures("key")
def test_a_job_on_a_board_joins_and_the_page_links_back(conn, data_dir):
    board = [{"id": "l-1", "text": "ML Engineer", "hostedUrl": "https://jobs.lever.co/deptofexamples/l-1",
              "categories": {"location": "Austin, TX"}}]  # fmt: skip
    url = "https://api.lever.co/v0/postings/departmentofexamples?mode=json"
    pages = {search(): reply(item()), url: board}
    run(conn, data_dir, pages, extra_boards=["lever:departmentofexamples"])
    job_id = one(conn, "SELECT id FROM jobs")[0]
    assert one(conn, "SELECT count(*) FROM jobs") == (1,)
    assert LINK in [source["via"] for source in queries.job(conn, job_id)["sources"]]


def test_remote_ok_still_reads_and_counts_as_complete():
    entry = {"company": "Acme", "position": "ML", "url": "https://remoteok.com/remote-jobs/1"}
    web = FakeWeb({remoteok.API: [{"legal": "x"}, entry]})
    with closing(fetch.Fetcher(httpx.MockTransport(web))) as fetcher:
        found, skipped, complete = remoteok.read(fetcher, ["ML"])
    assert (len(found), skipped, complete) == (1, 0, True)


def test_usajobs_is_a_feed_and_off_by_default():
    assert "usajobs" in FEEDS and FEEDS["usajobs"].LABEL == "USAJobs"
    assert SourceSettings().feeds == ["remoteok"]
