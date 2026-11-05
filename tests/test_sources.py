from contextlib import closing

import httpx
import pytest

from app.fetch import ALLOWED_HOSTS, Fetcher, FetchError
from app.sources import (
    Job,
    board_of,
    closed_site,
    day_from_timestamp,
    host_of,
    html_to_text,
    is_simplify_site,
    parse_each,
    plain_text,
    posting_of,
    required_text,
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://job-boards.greenhouse.io/acme/jobs/123", ("greenhouse", "acme", "123")),
        ("https://boards.greenhouse.io/acme/jobs/123?gh_jid=123", ("greenhouse", "acme", "123")),
        ("https://boards.greenhouse.io/embed/job_app?for=acme&token=123", ("greenhouse", "acme", "123")),
        ("https://jobs.lever.co/Acme/0a1b-2c3d/apply", ("lever", "acme", "0a1b-2c3d")),
        ("https://jobs.ashbyhq.com/Globex/9f8e-7d6c/application?embed=true", ("ashby", "globex", "9f8e-7d6c")),
    ],
)
def test_board_jobs_are_recognised(url, expected):
    assert posting_of(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://job-boards.eu.greenhouse.io/acme/jobs/123",  # European hosting: no public API
        "https://jobs.eu.lever.co/acme/0a1b-2c3d",
        "https://boards.greenhouse.io/embed/job_app?token=123",  # job number without a board
        "https://acme.example/careers/apply/?gh_jid=123",  # Greenhouse form on a company site
        "https://acme.wd1.myworkdayjobs.com/lever/job/1",  # Workday, despite the word "lever"
        "https://jobs.lever.co/acme",  # a board page, not a job
        "https://jobs.lever.co/..%2Fadmin/1",  # unsafe board name
        "https://simplify.jobs/p/123",
        "ftp://jobs.lever.co/acme/1",
    ],
)
def test_other_links_are_not_board_jobs(url):
    assert posting_of(url) is None


@pytest.mark.parametrize(
    ("url", "board"),
    [
        ("https://jobs.lever.co/Acme/0a1b-2c3d/apply", "lever:acme"),
        ("http://jobs.lever.co/acme", "lever:acme"),  # the board's own page counts too
        ("https://boards.greenhouse.io/embed/job_board?for=acme", "greenhouse:acme"),
        ("https://job-boards.greenhouse.io/acme/jobs/123", "greenhouse:acme"),
        ("https://jobs.ashbyhq.com/globex", "ashby:globex"),
        ("https://jobs.lever.co/", None),
        ("https://jobs.lever.co/" + "a" * 101, None),  # far too long to be a board name
        ("https://jobs.lever.co/..%2Fadmin", None),
        ("https://boards.greenhouse.io/embed/job_board", None),
        ("https://jobs.eu.lever.co/acme", None),  # no public API there
        ("https://acme.example/jobs.lever.co/acme", None),
    ],
)
def test_the_board_of_any_address_on_a_job_board_site(url, board):
    assert board_of(url) == board


def test_simplify_site_is_recognised_with_or_without_a_trailing_dot():
    for url in ("https://simplify.jobs/p/1", "https://SIMPLIFY.JOBS./p/1", "https://www.simplify.jobs/p/1"):
        assert is_simplify_site(url)
    assert not is_simplify_site("https://notsimplify.jobs/p/1")


def test_an_unclosed_script_does_not_swallow_the_rest():
    assert html_to_text("<p>Intro</p><script>x<p>Requirements: Python</p>") == "Intro\n\nx\nRequirements: Python"


def test_dates_in_seconds_or_milliseconds():
    assert day_from_timestamp(1792506468) == "2026-10-20"
    assert day_from_timestamp(1792506468000) == "2026-10-20"
    for impossible in (None, "2026-10-20", -5, 0, 10**30):
        assert day_from_timestamp(impossible) is None


def test_html_becomes_plain_text_and_scripts_are_dropped():
    html = (
        "<h2>About&nbsp;Acme</h2><p>We build   things.</p><script>alert('x')</script>"
        "<style>p {color: red}</style><ul><li>Python</li><li>RL</li></ul><p>Café 日本\0</p>"
    )
    assert html_to_text(html) == "About Acme\n\nWe build things.\n\n- Python\n\n- RL\n\nCafé 日本"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/jobs/view/1",
        "https://uk.linkedin.com/jobs/view/1",
        "https://LINKEDIN.COM/jobs",
        "https://linkedin.com./jobs",
        "https://linkedin.com:443/jobs",
        "http://linkedin.com/jobs",
        "https://user@linkedin.com/jobs",
        "https://uk.indeed.com/viewjob",
        "https://indeed.co.uk/viewjob",
        "https://indeed.com.au/viewjob",
        "https://de.indeed.com/viewjob",
        "https://indeed.de/viewjob",
        "https://www.indeed.com/viewjob",
        "https://app.joinhandshake.com/jobs/1",
        "https://asu.joinhandshake.com/jobs/1",
        "https://[x",  # an address that can't be read counts as closed
        "not an address",
    ],
)
def test_closed_sites(url):
    assert closed_site(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://notlinkedin.com/jobs",
        "https://linkedin.com.evil.com/jobs",
        "https://evil.com/linkedin.com",
        "https://evil.com?u=linkedin.com",
        "https://indeed.evil.com/jobs",
        "https://indeedjobs.com/jobs",
        "https://notjoinhandshake.com/jobs",
        "https://handshake.com/jobs",
        "https://jobs.lever.co/acme/1",
    ],
)
def test_look_alikes_are_not_closed_sites(url):
    assert not closed_site(url)


def test_html_and_plain_text_tidy_spaces_the_same_way_and_only_html_reads_tags():
    typed = "  under <5 years  \n\n\n\n and  more "
    assert plain_text(typed) == "under <5 years\n\nand more"
    assert html_to_text("<p>one   two</p>\n\n\n\n<p>three</p>") == "one two\n\nthree"


SOFT_HYPHEN, ZERO_WIDTH = "\u00ad", "\u200b"
# Spelled the way a browser still reads as the site: {} is the site's name with a dot in it, e.g. linkedin.com
BROWSER_FORMS = [
    "https://www.{}\\jobs\\view\\1",  # a backslash is a slash
    "https://www.{}\\@evil.example/",
    "HTTPS://WWW.{}/",
    "https://{}./jobs",
    "https://{}:443/jobs",
    "https://user:word@{}/jobs",
    "  https://{}/jobs  ",
]
SITES = ["linkedin.com", "uk.indeed.com", "indeed.co.uk", "app.joinhandshake.com", "lnkd.in", "linkedin.cn",
         "joinhandshake.co.uk"]  # fmt: skip
SPELLED = [
    "https://linked%69n.com/jobs",
    "https://www.indeed%2Ecom/jobs",
    "https://join%68andshake.com/jobs",
    "https://ｌｉｎｋｅｄｉｎ.com/jobs",  # full-width letters
    "https://ｉｎｄｅｅｄ.com/jobs",
    "https://linkedin。com/jobs",  # other full stops
    "https://linkedin．com/jobs",
    "https://linkedin｡com/jobs",
    "https://indeed。co。uk/jobs",
    "https://joinhandſhake.com/jobs",  # long s
    f"https://link{SOFT_HYPHEN}edin.com/jobs",
    f"https://linked{ZERO_WIDTH}in.com/jobs",
    f"https://in{SOFT_HYPHEN}deed.com/jobs",
    f"https://join{ZERO_WIDTH}handshake.com/jobs",
    "https://LinkedIn.CN/x",
    "https://uk.lnkd.in/abc",
    "https://app.joinhandshake.co.uk/jobs",
]
OPEN_SITES = [
    "https://evil.example\\@linkedin.com/",  # the backslash ends the site's name: a browser goes to evil.example
    "https://notlinkedin.com/jobs",
    "https://müller.example/jobs",
    "https://jobs.lever.co/acme/1",
    "https://lnkd.in.evil.com/x",
    "https://notlnkd.in/x",
    "https://linkedin.cn.evil.com/x",
    "https://notlinkedin.cn/x",
    "https://joinhandshake.co.uk.evil.com/x",
    "https://notjoinhandshake.co.uk/x",
    "https://lіnkedin.com/jobs",  # a Cyrillic letter: another site, however alike it looks
]


@pytest.mark.parametrize("url", [form.format(site) for form in BROWSER_FORMS for site in SITES] + SPELLED)
def test_closed_sites_as_a_browser_reads_them(url):
    assert closed_site(url)


@pytest.mark.parametrize("url", OPEN_SITES)
def test_addresses_a_browser_opens_elsewhere_are_not_closed_sites(url):
    assert not closed_site(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://simplify.jobs\\p\\1",
        "https://simplify.jobs\\@evil.example/",
        "https://simpl%69fy.jobs/p/1",
        "https://simplify%2Ejobs/p/1",
        "https://ｓｉｍｐｌｉｆｙ.jobs/p/1",
        "https://simplify。jobs/p/1",
        "https://ſimplify.jobs/p/1",
        f"https://simp{SOFT_HYPHEN}lify.jobs/p/1",
        f"https://simplify{ZERO_WIDTH}.jobs/p/1",
        "https://www.simplify.jobs./p/1",
        "https://a..b/x",  # can't be read: counts as simplify.jobs, so the app never opens it
    ],
)
def test_simplify_as_a_browser_reads_it(url):
    assert is_simplify_site(url)


@pytest.mark.parametrize(
    "url", ["https://notsimplify.jobs/1", "https://evil.example\\@simplify.jobs/", "not an address"]
)
def test_addresses_that_are_not_simplify(url):
    assert not is_simplify_site(url)


@pytest.mark.parametrize("url", ["https://a..b/x", "https://[x", "https://" + "a" * 64 + ".com/"])
def test_an_address_that_cannot_be_read_counts_as_closed(url):
    assert closed_site(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://linked%49n.com/x",  # a capital behind a percent sign
        "https://LINKED%49N.com/x",
        "https://%4cinkedin.com/x",
        "https://www.indeed%2Ecom/x",
        "https://linkedin.com%2Fjobs",  # no browser opens these: they can't be read, so they count as closed
        "https://linked%00in.com/",
    ],
)
def test_a_percent_sign_hides_nothing_from_the_guard(url):
    assert closed_site(url)


def test_a_capital_behind_a_percent_sign_is_still_simplify():
    assert is_simplify_site("https://simpl%49fy.jobs/p/1")


@pytest.mark.parametrize("url", ["https://[::1]/x", "https://my_site.example/x", "https://müller.example/x"])
def test_ordinary_hosts_stay_readable_and_open(url):
    assert host_of(url)
    assert not closed_site(url)
    assert not is_simplify_site(url)


def test_a_capital_behind_a_percent_sign_reads_as_the_same_name():
    assert host_of("https://my%53ite.example/x") == "mysite.example"


def test_no_address_the_app_may_download_from_is_a_closed_site():
    assert not [host for host in ALLOWED_HOSTS if closed_site(f"https://{host}/")]


@pytest.mark.parametrize(
    "url", ["https://www.linkedin.com/jobs", "https://www.indeed.com/jobs", "https://app.joinhandshake.com/"]
)
def test_the_app_itself_refuses_the_closed_sites(url):
    with closing(Fetcher(httpx.MockTransport(lambda request: httpx.Response(200)))) as fetcher:
        with pytest.raises(FetchError, match="address not allowed"):
            fetcher.get_text(url)


def test_a_job_drops_lone_surrogates_from_every_text():
    bad = "x\ud800y"
    job = Job(source=bad, source_job_id=bad, company=bad, title=bad, url=bad, locations=[bad],
              place_checks=[(bad, bad)], terms=[bad], description=bad, salary=bad)  # fmt: skip
    assert (job.company, job.locations, job.place_checks, job.terms) == ("xy", ["xy"], [("xy", "xy")], ["xy"])
    assert (job.title, job.url, job.description, job.salary, job.source_job_id) == ("xy",) * 5


@pytest.mark.parametrize("name", ["remote", "level", "category", "sponsorship", "date_posted"])
def test_every_other_text_field_is_cleaned_too(name):
    job = Job(source="s", source_job_id="1", company="c", title="t", url="u", locations=[], **{name: "x\ud800y"})
    assert getattr(job, name) == "xy"


def test_a_jobs_source_is_cleaned_too():
    assert Job(source="x\ud800y", source_job_id="1", company="c", title="t", url="u", locations=[]).source == "xy"


def test_a_job_keeps_what_is_not_text():
    job = Job(source="s", source_job_id="1", company="c", title="t", url="u", locations=[], listed=False)
    assert (job.listed, job.description, job.locations) == (False, None, [])


def test_an_entry_too_big_to_read_is_a_broken_entry():
    def to_job(entry):
        return float(entry)

    assert parse_each([10**400, 1], to_job) == ([1.0], 1)


@pytest.mark.parametrize("value", ["\ud800", " \ud800 ", "\ud800\udfff"])
def test_text_of_nothing_but_broken_characters_counts_as_empty(value):
    with pytest.raises(ValueError, match="company is empty"):
        required_text({"company": value}, "company")
