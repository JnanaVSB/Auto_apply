import pytest

from app.sources import day_from_timestamp, html_to_text, posting_of


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
