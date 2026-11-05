"""Hacker News in the phone-book refresh, with made-up threads, a fake web and a throwaway database."""

from contextlib import closing

import httpx
import pytest
import sqlcipher3
from helpers import FakeWeb, add_job, one, settings

from app import collect, fetch, phonebook
from app.config import SourceSettings
from app.sources import hn

pytestmark = pytest.mark.usefixtures("no_waiting")  # downloads never really wait

ON = SourceSettings()  # on unless you switch it off
OFF = SourceSettings(hacker_news=False)
NO_INDEX = {phonebook.CRAWLS: [{"id": "CC-MAIN-2026-40"}]}  # the crawl's index has nothing on any site
LEVER_ACME = "https://api.lever.co/v0/postings/acme?mode=json"
HIRING = "Ask HN: Who is hiring? (November 2026)"
THREAD = 100
NULL = httpx.Response(200, text="null")  # the API's answer for an id with nothing behind it


def story(number=THREAD, title=HIRING, kids=()):
    return {"id": number, "by": "whoishiring", "type": "story", "title": title, "kids": list(kids)}


def comment(number, *boards, text="We are hiring."):
    """A top-level comment in the API's shape, its links escaped and shortened as Hacker News does."""
    links = "".join(
        f'<a href="https:&#x2F;&#x2F;jobs.lever.co&#x2F;{board}" rel="nofollow">https:&#x2F;&#x2F;jobs.lev...</a>'
        for board in boards
    )
    return {"id": number, "by": "someone", "parent": THREAD, "type": "comment", "text": f"{text}<p>{links}"}


def thread(comments):
    """The pages of one hiring thread with these comments, by id."""
    posts = {hn.POSTS: {"submitted": [THREAD]}, hn.item(THREAD): story(kids=comments)}
    return NO_INDEX | posts | {hn.item(number): reply for number, reply in comments.items()}


def lever(number, title="ML Engineer"):
    return {"id": f"l-{number}", "text": title, "hostedUrl": f"https://jobs.lever.co/acme/l-{number}",
            "categories": {"location": "Austin, TX"}}  # fmt: skip


def refresh(conn, data_dir, web, sources=ON):
    lines = []
    config = settings(data_dir).model_copy(update={"sources": sources})
    with closing(fetch.Fetcher(httpx.MockTransport(web))) as fetcher:
        phonebook.refresh(conn, config, fetcher, lines.append)
    return lines


def asked(web):
    return [str(request.url) for request in web.requests if request.url.host == fetch.HN_API]


def names(conn):
    return {board for (board,) in conn.execute("SELECT board FROM phonebook WHERE found_by = 'hackernews'")}


def marks(conn):
    return {part for (part,) in conn.execute("SELECT part FROM phonebook_done WHERE part LIKE 'hn:%'")}


def outcomes(conn):
    return [outcome for (outcome,) in conn.execute("SELECT outcome FROM phonebook_runs ORDER BY id")]


def stored(conn):
    """Every value in every table of the database, as one text."""
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    rows = [conn.execute(f'SELECT * FROM "{name}"').fetchall() for (name,) in tables]  # noqa: S608 (names from the database)
    return "\n".join(str(value) for table in rows for row in table for value in row)


def test_a_long_read_says_how_far_it_has_got(conn, data_dir, monkeypatch):
    monkeypatch.setattr(phonebook, "PROGRESS_EVERY", 2)
    lines = refresh(conn, data_dir, FakeWeb(thread({number: comment(number) for number in (1, 2, 3)})))
    assert lines[:2] == [
        "  2 Hacker News comments read",
        "Hacker News: 3 new comments read, 0 with a readable board link, 0 new names",
    ]


def test_a_thread_gives_names_and_the_refresh_says_what_it_gained(conn, data_dir):
    comments = {1: comment(1, "acme"), 2: comment(2), 3: comment(3, "ACME", "acme")}
    pages = thread(comments) | {LEVER_ACME: [lever(1), lever(2, "ML Researcher"), lever(3, "Accountant")]}
    lines = refresh(conn, data_dir, FakeWeb(pages))
    assert lines[0] == "Hacker News: 3 new comments read, 2 with a readable board link, 1 new names"
    assert lines[-1].startswith("Checked 3 names: 1 added")  # the page shows only the last line, so one line holds both
    assert "to confirm on the Boards page" in lines[-1]
    assert lines[-1].endswith(
        "(asked again next time). Boards from Hacker News so far: 1; 2 of your matching jobs came from them"
    )  # the tally counts what the check just added
    assert one(conn, "SELECT outcome FROM phonebook_runs") == ("done",)


def test_a_name_is_read_only_once_its_site_confirms_it(conn, data_dir):
    pages = thread({1: comment(1, "acme", "gone")}) | {LEVER_ACME: [lever(1)]}
    refresh(conn, data_dir, FakeWeb(pages))
    assert conn.execute("SELECT name, found_by FROM sources").fetchall() == [("lever:acme", "hackernews")]
    assert one(conn, "SELECT result FROM phonebook WHERE board = 'lever:gone'") == ("not found",)
    assert one(conn, "SELECT count(*) FROM job_sources WHERE source = 'lever:acme'") == (1,)


def test_only_the_two_newest_hiring_threads_are_read_and_no_older_post_is_asked_for(conn, data_dir):
    pages = NO_INDEX | {
        hn.POSTS: {"submitted": [3, 7, 4, 5, 6]},
        hn.item(7): story(7, "Ask HN: Who wants to be hired? (November 2026)"),
        hn.item(6): story(6, kids=[61]),
        hn.item(5): story(5, "Ask HN: Freelancer? Seeking freelancer? (November 2026)", kids=[51]),
        hn.item(4): story(4, "Ask HN: Who is hiring? (October 2026)", kids=[41]),
        hn.item(3): story(3, "Ask HN: Who is hiring? (September 2026)", kids=[31]),
        hn.item(61): comment(61, "newest"),
        hn.item(41): comment(41, "older"),
    }
    web = FakeWeb(pages)
    refresh(conn, data_dir, web)
    order = [hn.item(number) for number in (7, 6, 5, 4, 61, 41)]
    assert asked(web) == [hn.POSTS, *order]  # replies are never asked for: only each thread's kids
    assert names(conn) == {"lever:newest", "lever:older"}


@pytest.mark.parametrize("posts", [[], [1]])
def test_no_hiring_thread_is_one_line_and_the_rest_of_the_refresh_goes_on(conn, data_dir, posts):
    with conn:
        job = add_job(conn, "ML Engineer")
        conn.execute(phonebook.ADD_NAME, {"board": "lever:acme", "found_by": "list:a/b", "on": "x", "cross_of": None})
    pages = NO_INDEX | {
        hn.POSTS: {"submitted": posts},
        hn.item(1): story(1, "Ask HN: Who wants to be hired? (November 2026)"),
        LEVER_ACME: [lever(1)],
    }
    web = FakeWeb(pages)
    lines = refresh(conn, data_dir, web)
    assert lines[0] == (
        "Hacker News could not be read (no 'Who is hiring?' thread among its newest posts). Names found so far are "
        "kept; tried again next time."
    )
    assert phonebook.CRAWLS in [str(request.url) for request in web.requests]  # Common Crawl was still read
    assert one(conn, "SELECT result FROM phonebook WHERE board = 'lever:acme'") == ("added",)  # and the names checked
    assert one(conn, "SELECT status FROM jobs WHERE id = ?", job) == ("open",)
    assert outcomes(conn) == ["partial"]


@pytest.mark.parametrize(
    ("pages", "reason"),
    [
        ({hn.POSTS: {"nothing": 1}}, "its list of threads looks different from usual"),
        ({hn.POSTS: 503}, "busy or failing (status 503); gave up after 3 retries"),
        (
            {hn.POSTS: {"submitted": [THREAD]}, hn.item(THREAD): story() | {"kids": "sam@example.com"}},
            "its list of comments looks different from usual",
        ),
    ],
)
def test_a_reply_that_cannot_be_used_ends_the_read_with_one_honest_line(conn, data_dir, pages, reason):
    lines = refresh(conn, data_dir, FakeWeb(NO_INDEX | pages))
    assert lines[0] == f"Hacker News could not be read ({reason}). Names found so far are kept; tried again next time."
    assert outcomes(conn) == ["partial"]


def test_a_pause_between_comments_continues_where_it_stopped(conn, data_dir, monkeypatch):
    pages = thread({number: comment(number, f"co{number}") for number in (1, 2, 3)})
    first = FakeWeb(pages)
    monkeypatch.setattr(collect, "collection_waiting", lambda _: hn.item(2) in asked(first))
    assert refresh(conn, data_dir, first) == [phonebook.PAUSED]
    assert names(conn) == {"lever:co1", "lever:co2"}
    monkeypatch.setattr(collect, "collection_waiting", lambda _: False)
    second = FakeWeb(pages)
    refresh(conn, data_dir, second)
    assert asked(second) == [hn.POSTS, hn.item(THREAD), hn.item(3)]  # each comment read once in all
    assert names(conn) == {"lever:co1", "lever:co2", "lever:co3"}
    assert outcomes(conn) == ["paused", "done"]


def test_a_crash_while_saving_keeps_neither_the_names_nor_the_mark(conn, data_dir, monkeypatch):
    pages = thread({1: comment(1, "acme")})
    with monkeypatch.context() as patch, pytest.raises(sqlcipher3.Error):
        patch.setattr(phonebook, "DONE", "INSERT INTO no_such_table VALUES (?, ?)")  # the names were just added
        refresh(conn, data_dir, FakeWeb(pages))
    assert (names(conn), marks(conn)) == (set(), set())
    web = FakeWeb(pages)
    refresh(conn, data_dir, web)
    assert asked(web).count(hn.item(1)) == 1  # read again, and this time saved
    assert (names(conn), marks(conn)) == ({"lever:acme"}, {"hn:1"})


def test_new_comments_in_a_thread_already_read_are_read_next_time_and_only_they(conn, data_dir):
    refresh(conn, data_dir, FakeWeb(thread({1: comment(1, "old")})))
    web = FakeWeb(thread({1: comment(1, "old"), 2: comment(2, "new")}))
    lines = refresh(conn, data_dir, web)
    assert asked(web) == [hn.POSTS, hn.item(THREAD), hn.item(2)]
    assert lines[0] == "Hacker News: 1 new comments read, 1 with a readable board link, 1 new names"


def test_a_comment_that_fails_stops_the_read_and_the_next_refresh_starts_there(conn, data_dir):
    comments = {number: comment(number, f"co{number}") for number in range(1, 11)}
    lines = refresh(conn, data_dir, FakeWeb(thread(comments) | {hn.item(5): 500}))
    assert lines[0] == (
        "Hacker News could not be read (busy or failing (status 500); gave up after 3 retries). Names found so far "
        "are kept; tried again next time."
    )
    assert names(conn) == {f"lever:co{number}" for number in range(1, 5)}
    assert outcomes(conn) == ["partial"]
    web = FakeWeb(thread(comments))
    refresh(conn, data_dir, web)
    assert asked(web) == [hn.POSTS, hn.item(THREAD), *map(hn.item, range(5, 11))]


def test_a_comment_that_looks_changed_is_said_and_asked_for_again_and_the_good_ones_are_kept(conn, data_dir):
    comments = {1: comment(1, "one"), 2: comment(2, "two"), 3: comment(3, "three"), 4: {"id": 4, "by": "someone"}}
    lines = refresh(conn, data_dir, FakeWeb(thread(comments)))
    assert lines[0] == (
        "Hacker News could not be read (1 comments look different from usual). Names found so far are kept; tried "
        "again next time."
    )
    assert marks(conn) == {"hn:1", "hn:2", "hn:3"}
    assert names(conn) == {"lever:one", "lever:two", "lever:three"}
    assert outcomes(conn) == ["partial"]
    web = FakeWeb(thread(comments))
    refresh(conn, data_dir, web)
    assert asked(web) == [hn.POSTS, hn.item(THREAD), hn.item(4)]


def test_the_comments_after_one_that_looks_changed_are_still_read(conn, data_dir):
    comments = {1: {}, 2: comment(2, "two")}
    refresh(conn, data_dir, FakeWeb(thread(comments)))
    assert marks(conn) == {"hn:2"}


def test_a_thread_whose_comment_ids_are_all_odd_is_said_not_read_as_empty(conn, data_dir):
    pages = NO_INDEX | {hn.POSTS: {"submitted": [THREAD]}, hn.item(THREAD): story(kids=["a", True])}
    lines = refresh(conn, data_dir, FakeWeb(pages))
    assert lines[0].startswith("Hacker News could not be read (its list of comments looks different from usual)")
    assert outcomes(conn) == ["partial"]


def test_a_comment_the_api_says_is_not_there_is_nothing_there_and_the_read_goes_on(conn, data_dir):
    comments = {number: comment(number, f"co{number}") for number in range(1, 11)}
    lines = refresh(conn, data_dir, FakeWeb(thread(comments) | {hn.item(5): 404}))
    assert lines[0] == "Hacker News: 10 new comments read, 9 with a readable board link, 9 new names"
    assert "hn:5" in marks(conn)
    assert outcomes(conn) == ["done"]


def test_deleted_and_empty_comments_are_marked_done_and_not_asked_for_again(conn, data_dir):
    comments = {1: {"id": 1, "deleted": True}, 2: NULL, 3: comment(3, "acme") | {"dead": True}}
    lines = refresh(conn, data_dir, FakeWeb(thread(comments)))
    assert lines[0] == "Hacker News: 3 new comments read, 0 with a readable board link, 0 new names"
    assert marks(conn) == {"hn:1", "hn:2", "hn:3"}
    web = FakeWeb(thread(comments))
    refresh(conn, data_dir, web)
    assert asked(web) == [hn.POSTS, hn.item(THREAD)]


def test_switched_off_means_no_request_and_no_line(conn, data_dir):
    web = FakeWeb(thread({1: comment(1, "acme")}))
    lines = refresh(conn, data_dir, web, OFF)
    assert asked(web) == []
    assert not [line for line in lines if "Hacker News" in line]
    assert one(conn, "SELECT outcome FROM phonebook_runs") == ("done",)


def test_a_collection_never_contacts_hacker_news(conn, data_dir):
    web = FakeWeb({})
    config = settings(data_dir).model_copy(update={"sources": ON})
    with closing(fetch.Fetcher(httpx.MockTransport(web))) as fetcher:
        collect.collect(conn, config, fetcher, [].append)
    assert web.requests and not asked(web)


def test_the_internet_check_never_knocks_on_hacker_news(conn, data_dir):
    web = FakeWeb({}, down=fetch.COLLECT_HOSTS)  # every site is tried before giving up
    with pytest.raises(collect.CollectError, match="No internet connection"):
        refresh(conn, data_dir, web)
    assert web.requests and not asked(web)


def test_nothing_from_a_comment_is_kept_but_the_board_name(conn, data_dir):
    secrets = ("sam.fakename@example.com", "Sam Fakename", "We pay our staff in marbles", "fake_author_77")
    text = f"{secrets[1]}, {secrets[0]}. {secrets[2]}."
    pages = thread({1: comment(1, "acme", text=text) | {"by": secrets[3]}}) | {LEVER_ACME: [lever(1)]}
    lines = refresh(conn, data_dir, FakeWeb(pages))
    everything = stored(conn) + "\n".join(lines)
    assert "lever:acme" in everything  # the search does reach what was saved
    assert not [secret for secret in secrets if secret in everything]


def test_a_known_name_is_not_new_and_keeps_its_first_finder(conn, data_dir):
    with conn:
        conn.execute("INSERT INTO sources (name, found_by) VALUES ('lever:known', 'simplify')")
        values = {"board": "lever:waiting", "found_by": "commoncrawl", "on": "2026-10-01", "cross_of": None}
        conn.execute(phonebook.ADD_NAME, values)
    lines = refresh(conn, data_dir, FakeWeb(thread({1: comment(1, "known", "waiting")})))
    assert lines[0] == "Hacker News: 1 new comments read, 1 with a readable board link, 0 new names"
    assert one(conn, "SELECT found_by FROM sources WHERE name = 'lever:known'") == ("simplify",)
    assert conn.execute("SELECT board, found_by FROM phonebook").fetchall() == [("lever:waiting", "commoncrawl")]


def test_the_tally_counts_only_hacker_news_boards_and_each_job_once(conn):
    with conn:
        boards = [("lever:one", "hackernews"), ("ashby:two", "hackernews"), ("lever:other", "simplify")]
        conn.executemany("INSERT INTO sources (name, found_by) VALUES (?, ?)", boards)
        add_job(conn, "ML Engineer", sources=["lever:one", "ashby:two"])  # one job on two of its boards
        add_job(conn, "ML Intern", sources=["lever:one"])
        add_job(conn, "ML Researcher", sources=["lever:other"])
    assert phonebook.hn_so_far(conn) == "Boards from Hacker News so far: 2; 2 of your matching jobs came from them"


def test_the_tally_starts_at_nothing(conn):
    assert phonebook.hn_so_far(conn) == "Boards from Hacker News so far: 0; 0 of your matching jobs came from them"
