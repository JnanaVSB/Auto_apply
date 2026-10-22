"""The job page: every detail, every board or list with its own state, and safe display, with made-up jobs."""

from urllib.parse import quote

import pytest
from test_web_jobs import add_job, conn  # noqa: F401 (shared helper and fixture)
from test_web_security import client, clock, data_dir, log_in, quick_hash  # noqa: F401 (shared fixtures)

from app.web import queries


@pytest.fixture
def page(client, conn):  # noqa: F811
    """Log in, then open a job's page by its number."""
    log_in(client)

    def open_job(job_id, address=""):
        return client.get(f"/jobs/{job_id}{address}")

    return open_job


def test_every_detail_is_shown(conn, page):  # noqa: F811
    with conn:
        job_id = add_job(
            conn, "ML Intern", company="Acme", locations='["Austin, TX", "Remote", "Boston, MA"]',
            remote="hybrid", sponsorship="offers", terms='["Summer 2027", "Fall 2027"]', role_category="AI/ML/Data",
            salary="$50/hr", date_posted="2026-10-10", us_location="no", check_location=1, location_by_you=1,
            added_by_you=1, not_interested=1, description="Train models.",
        )  # fmt: skip
    text = page(job_id).text
    for shown in ("ML Intern", "Acme", "Austin, TX; Remote; Boston, MA", "hybrid", "offers", "Summer 2027, Fall 2027",
                  "AI/ML/Data", "$50/hr", "2026-10-10", "not US", "please check location", "location set by you",
                  "added by you", "not interested", "Train models.", "open"):  # fmt: skip
        assert shown in text


def test_every_source_is_shown_with_its_own_state(conn, page):  # noqa: F811
    with conn:
        job_id = add_job(conn, "ML Intern", sources=(), status="no longer available", status_reason="board shut down")
        conn.executemany(
            "INSERT INTO job_sources (source, source_job_id, job_id, listed, unlisted_on, unlisted_reason) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("greenhouse:acme", "1", job_id, 1, None, None),
                ("lever:gone", "2", job_id, 0, "2026-10-20", "board shut down"),
                ("simplify:Summer2027-Internships", "3", job_id, 0, None, "reason not recorded (before 2026-10-21)"),
                ("manual", "https://acme.example/1", job_id, 1, None, None),
            ],
        )
        conn.executemany(
            "INSERT INTO sources (name, last_ok, retired_on) VALUES (?, ?, ?)",
            [("greenhouse:acme", "2026-10-22T08:00:00+00:00", None), ("lever:gone", None, "2026-10-20")],
        )
    text = page(job_id).text
    assert "no longer available: board shut down" in text
    assert 'Greenhouse board &#39;acme&#39;: listed <span class="muted">(last read 2026-10-22)</span>' in text
    assert "Lever board &#39;gone&#39;: board shut down (since 2026-10-20)" in text
    assert "board no longer read since 2026-10-20" in text
    assert "Simplify Summer2027-Internships: reason not recorded (before 2026-10-21)</li>" in text
    assert "added by you: listed</li>" in text


def test_a_missing_description_says_so(conn, page):  # noqa: F811
    with conn:
        job_id = add_job(conn, "Data Scientist")
    assert "Description not fetched yet" in page(job_id).text


def test_job_text_is_shown_as_plain_text_with_its_line_breaks(conn, page):  # noqa: F811
    with conn:
        job_id = add_job(conn, "<script>alert(1)</script>", description="Line one\n\n<b>Line</b> two")
    text = page(job_id).text
    assert "<script>alert(1)" not in text and "&lt;script&gt;alert(1)&lt;/script&gt;" in text
    assert 'class="description">Line one\n\n&lt;b&gt;Line&lt;/b&gt; two</div>' in text


@pytest.mark.parametrize(
    ("url", "manual", "link"),
    [
        ("https://simplify.jobs/p/123", 1, False),  # never a link, whatever it looks like
        ("https://acme.example/jobs/1", 0, True),
        ("javascript:alert(1)", 0, False),
    ],
)
def test_only_web_links_are_clickable_and_simplify_jobs_never(conn, page, url, manual, link):  # noqa: F811
    with conn:
        job_id = add_job(conn, "ML Intern", apply_url=url, open_manually_only=manual)
    text = page(job_id).text
    assert (f'href="{url}"' in text) is link
    assert ("the app never opens this site" in text) is bool(manual)
    if link:
        assert 'target="_blank" rel="noopener noreferrer"' in text


@pytest.mark.parametrize("address", ["999", "abc", "-1", "99999999999999999999", "1.5"])
def test_a_job_that_does_not_exist_is_page_not_found(page, address):
    response = page(address)
    assert (response.status_code, "Page not found." in response.text) == (404, True)


@pytest.mark.parametrize(
    ("back", "link"),
    [
        ("/?f=1&level=intern&page=2", "/?f=1&amp;level=intern&amp;page=2"),
        ("https://evil.example/", "/"),  # another site: the first view instead
        ("//evil.example/", "/"),
        ("/settings", "/"),
    ],
)
def test_back_to_the_list_keeps_only_the_dashboards_own_addresses(conn, page, back, link):  # noqa: F811
    with conn:
        job_id = add_job(conn, "ML Intern")
    assert f'<a href="{link}">Back to the list</a>' in page(job_id, f"?back={quote(back, safe='')}").text


def test_the_list_links_each_job_with_the_way_back(conn, page, client):  # noqa: F811
    with conn:
        job_id = add_job(conn, "ML Intern", level="mid")
    text = client.get("/?f=1&level=mid").text
    assert f'href="/jobs/{job_id}?back=/%3Ff%3D1%26level%3Dmid%26status%3D"' not in text  # status ticked off: any
    assert f'href="/jobs/{job_id}?back=/%3Ff%3D1%26level%3Dmid"' in text


def test_the_job_page_needs_a_login(conn, client):  # noqa: F811
    with conn:
        job_id = add_job(conn, "ML Intern")
    assert client.get(f"/jobs/{job_id}", follow_redirects=False).headers["location"] == "/login"


def test_job_query_reads_one_job(conn):  # noqa: F811
    with conn:
        job_id = add_job(conn, "ML Intern")
    assert queries.job(conn, job_id)["sources"] == [
        {"label": "Greenhouse board 'acme'", "state": "listed", "board": ""}
    ]
    assert queries.job(conn, job_id + 1) is None
