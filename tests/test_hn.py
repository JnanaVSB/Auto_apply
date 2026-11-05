"""Reading Hacker News' replies: which threads, which comments, which links. All made up; nothing is downloaded."""

import pytest

from app.fetch import HN_API
from app.sources import SourceError, hn

HIRING = "Ask HN: Who is hiring? (November 2026)"
ODD_NUMBERS = [True, 1.5, "1/../2", 0, -5, None, 7]


def story(title=HIRING, **changes):
    return {"id": 1, "by": "whoishiring", "type": "story", "title": title} | changes


def comment(text, **changes):
    return {"id": 2, "by": "someone", "type": "comment", "text": text} | changes


def link(url):
    return f'<a href="{url}" rel="nofollow">{url}</a>'


def test_addresses_are_on_the_official_api():
    assert hn.POSTS == f"https://{HN_API}/v0/user/whoishiring.json"
    assert hn.item(42) == f"https://{HN_API}/v0/item/42.json"


def test_the_hiring_threads_are_picked_among_the_other_kinds():
    titles = [
        "Ask HN: Who is hiring? (November 2026)",
        "Ask HN: Who wants to be hired? (November 2026)",
        "Ask HN: Freelancer? Seeking freelancer? (November 2026)",
        "Ask HN: Who is hiring? (October 2026)",
        "Ask HN: Who wants to be hired? (October 2026)",
        "Ask HN: Who is hiring? (September 2026)",
    ]
    picked = [title for title in titles if hn.is_hiring(story(title))]
    assert picked == [titles[0], titles[3], titles[5]]


@pytest.mark.parametrize(
    "post",
    [
        story("Re: Ask HN: Who is hiring? (November 2026)"),
        story("ask hn: who is hiring? (november 2026)"),
        story(5),
        story(type="comment"),
        story(deleted=True),
        story(dead=True),
        {"id": 1, "type": "story"},
        None,
        [],
    ],
)
def test_anything_else_is_no_hiring_thread(post):
    assert hn.is_hiring(post) is False


def test_the_newest_posts_come_first_by_number():
    assert hn.newest_posts({"submitted": [3, 15, 7]}) == [15, 7, 3]


def test_at_most_twelve_posts_are_looked_at():
    assert hn.newest_posts({"submitted": list(range(1, 21))}) == list(range(20, 8, -1))


def test_only_positive_whole_numbers_can_reach_an_address():
    assert hn.newest_posts({"submitted": ODD_NUMBERS}) == [7]
    assert hn.comments_of({"kids": ODD_NUMBERS}) == [7]


@pytest.mark.parametrize("user", [None, [], {}, {"submitted": "sam@example.com"}])
def test_a_changed_user_reply_is_refused_without_repeating_it(user):
    with pytest.raises(SourceError, match="^its list of threads looks different from usual$"):
        hn.newest_posts(user)


def test_a_thread_with_no_comments_yet_has_none():
    assert hn.comments_of(story()) == []


def test_a_changed_thread_reply_is_refused_without_repeating_it():
    with pytest.raises(SourceError, match="^its list of comments looks different from usual$"):
        hn.comments_of(story(kids="sam@example.com"))


def test_at_most_a_thousand_comments_are_read():
    assert hn.comments_of(story(kids=list(range(1, 1501)))) == list(range(1, 1001))


def test_a_shortened_link_is_read_from_its_escaped_href_not_its_visible_text():
    text = (
        '<a href="https:&#x2F;&#x2F;jobs.ashbyhq.com&#x2F;acme-robotics" rel="nofollow">'
        "https:&#x2F;&#x2F;jobs.ashbyhq.com&#x2F;ac...</a>"
    )
    assert hn.boards_in(comment(text)) == {"ashby:acme-robotics"}


def test_a_link_with_no_rel_is_read_too():
    text = '<a href="https:&#x2F;&#x2F;jobs.lever.co&#x2F;acme">Apply</a>'  # some real links come this way
    assert hn.boards_in(comment(text)) == {"lever:acme"}


@pytest.mark.parametrize(
    "text",
    [
        "Apply at https://jobs.lever.co/acme",  # plain text, not a link
        "&lt;a href=&quot;https://jobs.lever.co/evil&quot;&gt;",  # HTML a person typed: it arrives escaped
    ],
)
def test_only_real_links_are_read(text):
    assert hn.boards_in(comment(text)) == set()


def test_greenhouses_form_link_keeps_its_escaped_query():
    text = '<a href="https://boards.greenhouse.io/embed/job_app?token=1&amp;for=acme">Apply</a>'
    assert hn.boards_in(comment(text)) == {"greenhouse:acme"}


def test_a_broken_address_is_no_link_and_the_others_are_still_read():
    text = link("https://[broken/acme") + " and " + link("https://jobs.lever.co/acme")
    assert hn.boards_in(comment(text)) == {"lever:acme"}


@pytest.mark.parametrize(
    "url",
    [
        "https://jobs.lever.co.evil.com/acme",
        "https://evil.com/jobs.lever.co/acme",
        "https://notjobs.lever.co/acme",
        "https://grnh.se/abc",  # a short link: where it leads is unknown
        "mailto:sam@example.com",
        "https://jobs.lever.co/%2e%2e",
        "https://jobs.lever.co/..",
        "https://jobs.lever.co/acme<script>",
        "https://jobs.lever.co/acme&lt;script&gt;",
    ],
)
def test_a_link_that_is_no_board_gives_no_name(url):
    assert hn.boards_in(comment(link(url))) == set()


def test_capitals_and_repeats_give_one_name():
    text = link("https://JOBS.LEVER.CO/ACME") + link("https://jobs.lever.co/acme/1234-abcd")
    assert hn.boards_in(comment(text)) == {"lever:acme"}


@pytest.mark.parametrize(
    "item",
    [
        None,
        [link("https://jobs.lever.co/acme")],
        5,
        comment(5),
        comment(None),
        comment(link("https://jobs.lever.co/acme"), deleted=True),
        comment(link("https://jobs.lever.co/acme"), dead=True),
    ],
)
def test_a_comment_that_is_gone_or_odd_gives_nothing(item):
    assert hn.boards_in(item) == set()
