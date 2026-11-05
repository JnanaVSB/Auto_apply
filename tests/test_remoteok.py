"""Remote OK's feed: what a reply may and may not hold. All made up; nothing is downloaded."""

from contextlib import closing
from dataclasses import replace
from urllib.parse import urlsplit

import httpx
import pytest
from helpers import FakeWeb

from app import fetch, jobs
from app.sources import SourceError, remoteok
from app.sources.boards import BOARDS
from app.sources.feeds import FEEDS

NOTICE = {"last_updated": 1792506468, "legal": "Made-up terms notice."}
LINK = "https://remoteok.com/remote-jobs/remote-ml-engineer-acme-1130000"


def listing(**changes):
    entry = {
        "slug": "remote-ml-engineer-acme-1130000",
        "id": "1130000",
        "epoch": 1792506468,
        "date": "2026-10-19T11:17:24+00:00",
        "company": "Acme",
        "position": "ML Engineer",
        "tags": ["python"],
        "description": "<p>Train <b>models</b>.</p>",
        "location": "Austin, TX",
        "salary_min": 120000,
        "salary_max": 150000,
        "url": "https://remoteOK.com/remote-jobs/remote-ml-engineer-acme-1130000",
    }
    return entry | changes


def without(name):
    return {key: value for key, value in listing().items() if key != name}


def one_job(**changes):
    found, skipped = remoteok.parse([NOTICE, listing(**changes)])
    assert skipped == 0
    return found[0]


def test_a_listing_becomes_a_job():
    job = one_job()
    assert (job.source, job.source_job_id, job.url) == ("feed:remoteok", LINK, LINK)
    assert (job.company, job.title, job.locations) == ("Acme", "ML Engineer", ["Austin, TX"])
    assert (job.remote, job.listed, job.description) == ("remote", True, "Train models.")
    assert (job.salary, job.date_posted) == ("USD 120,000–150,000 per year", "2026-10-19")


def test_the_link_is_the_same_whatever_capitals_or_extras_it_came_with():
    assert one_job(url=f"https://RemoteOK.com{LINK[20:]}?ref=something").source_job_id == LINK
    assert jobs.clean_link(one_job()) == LINK


@pytest.mark.parametrize(
    "reply",
    [
        [listing(), listing()],  # no notice first: its terms may have changed
        [{"last_updated": 1}, listing()],
        [5, listing()],
        {"jobs": [listing()]},
        None,
        [],
        [NOTICE],  # no jobs at all must never close every job
    ],
)
def test_a_reply_that_is_not_the_usual_one_is_refused(reply):
    with pytest.raises(SourceError):
        remoteok.parse(reply)


@pytest.mark.parametrize(
    "broken",
    [
        listing(url="javascript:alert(1)"),
        listing(url="http://remoteok.com/remote-jobs/1"),
        listing(url="https://remoteok.com.evil.com/x"),
        listing(url="https://evil.com/remoteok.com"),
        listing(url="https://user@remoteok.com/x"),
        listing(url="https://remoteok.com:8443/x"),
        listing(url="https://[broken/x"),
        listing(url="https://remoteok.com/"),
        listing(url=5),
        without("url"),
        listing(position="  "),
        listing(position=None),
        without("position"),
        listing(company=""),
        without("company"),
        "not an entry",
    ],
)
def test_a_broken_entry_is_counted_and_never_saved_and_the_rest_are_read(broken):
    found, skipped = remoteok.parse([NOTICE, broken, listing()])
    assert ([job.source_job_id for job in found], skipped) == ([LINK], 1)


@pytest.mark.parametrize("name", ["company", "position"])
def test_a_name_blank_once_cleaned_is_a_broken_entry(name):
    found, skipped = remoteok.parse([NOTICE, listing(**{name: "&#32;"}), listing()])
    assert (len(found), skipped) == (1, 1)


@pytest.mark.parametrize("name", ["company", "position"])
def test_a_name_of_nothing_but_broken_characters_is_a_broken_entry(name):
    found, skipped = remoteok.parse([NOTICE, listing(**{name: "\ud800"}), listing()])
    assert (len(found), skipped) == (1, 1)


def test_a_time_too_big_to_read_skips_only_its_entry():
    found, skipped = remoteok.parse([NOTICE, listing(date=None, epoch=10**400), listing()])
    assert (len(found), skipped) == (1, 1)


def test_every_feeds_address_is_on_its_allowed_list():
    assert all(urlsplit(feed.API).hostname in fetch.FEED_HOSTS for feed in FEEDS.values())


def test_every_entry_broken_is_refused():
    with pytest.raises(SourceError, match="none of its 2 entries"):
        remoteok.parse([NOTICE, listing(url="javascript:alert(1)"), without("position")])


@pytest.mark.parametrize(("location", "places"), [("", []), ("  ", []), (None, []), (5, []), (["Austin"], [])])
def test_no_usable_location_means_no_places_and_the_job_is_kept_as_unclear(location, places):
    job = one_job(location=location)
    assert job.locations == places
    assert jobs.us_location(job) == "unclear"


@pytest.mark.parametrize("place", ["Worldwide", "Remote"])
def test_places_that_name_no_country_are_kept_as_they_came(place):
    job = one_job(location=place)
    assert job.locations == [place]
    assert jobs.us_location(job) != "no"


def test_a_missing_location_counts_as_none():
    found, _ = remoteok.parse([NOTICE, without("location")])
    assert found[0].locations == []


@pytest.mark.parametrize(
    ("low", "high"),
    [(0, 0), (0, 150000), (120000, 0), (None, 150000), (120000, None), ("120000", 150000), (True, 150000),
     (150000, 120000), (-1, 150000), (30, 36), (9999, 150000), (9999, 10000)],
)  # fmt: skip
def test_pay_needs_both_ends_to_look_like_a_year_and_in_order(low, high):
    assert one_job(salary_min=low, salary_max=high).salary is None


@pytest.mark.parametrize("end", [float("inf"), 1e308, 10**400, 10_000_001])
def test_pay_past_any_real_salary_gives_none(end):
    assert one_job(salary_min=120000, salary_max=end).salary is None


def test_the_highest_yearly_pay_is_shown():
    assert one_job(salary_min=10_000_000, salary_max=10_000_000).salary == "USD 10,000,000–10,000,000 per year"


def test_the_lowest_yearly_pay_is_shown():
    assert one_job(salary_min=10000, salary_max=10000).salary == "USD 10,000–10,000 per year"


@pytest.mark.parametrize(
    ("date", "epoch", "day"),
    [
        ("2026-10-19T11:17:24+00:00", 1, "2026-10-19"),
        ("not a date", 1792506468, "2026-10-20"),
        (None, 1792506468, "2026-10-20"),
        ("not a date", "not a time", None),
        (5, None, None),
    ],
)
def test_the_date_comes_from_date_else_epoch_else_nowhere(date, epoch, day):
    assert one_job(date=date, epoch=epoch).date_posted == day


@pytest.mark.parametrize(
    ("description", "text"),
    [
        ("<p>Hello</p><script>alert(1)</script>", "Hello"),
        ("Bad\0byte", "Badbyte"),
        (None, None),
        (5, None),
        ("<script>alert(1)</script>", None),
        ("&lt;p&gt;Hello&lt;/p&gt;", "Hello"),  # some listings are escaped twice
        ("&lt;p&gt;Hello&lt;/p&gt;&lt;script&gt;alert(1)&lt;/script&gt;", "Hello"),
    ],
)
def test_a_description_is_plain_text_or_nothing(description, text):
    assert one_job(description=description).description == text


def test_escaped_names_and_places_arrive_as_people_read_them():
    job = one_job(company="A &amp; B", position="R&amp;D Engineer", location="Rio &amp; S&#227;o Paulo")
    assert (job.company, job.title, job.locations) == ("A & B", "R&D Engineer", ["Rio & São Paulo"])


def test_an_escaped_name_gives_the_same_match_key_as_the_plain_one():
    escaped = one_job(company="A &amp; B", position="R&amp;D Engineer")
    plain = replace(escaped, source="greenhouse:ab", company="A & B", title="R&D Engineer")
    assert jobs.match_key(escaped).split("|")[:2] == jobs.match_key(plain).split("|")[:2] == ["ab", "rdengineer"]


def test_read_asks_the_feed_itself():
    web = FakeWeb({remoteok.API: [NOTICE, listing()]})
    with closing(fetch.Fetcher(httpx.MockTransport(web))) as fetcher:
        found, skipped = remoteok.read(fetcher, jobs.Roles(frozenset(), None))
    assert ([job.source_job_id for job in found], skipped) == ([LINK], 0)


def test_every_feed_has_a_label_and_a_reader_and_none_is_a_board():
    assert not FEEDS.keys() & BOARDS.keys()
    for feed in FEEDS.values():
        assert feed.LABEL and callable(feed.read)
