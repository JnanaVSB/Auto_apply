"""Greenhouse, Lever and Ashby replies, as small made-up samples in each API's real shape (fake companies)."""

import pytest

from app.sources import SourceError, ashby, greenhouse, lever


def greenhouse_job(**changes):
    job = {
        "id": 123,
        "title": "Research Scientist, RL",
        "company_name": "Acme",
        "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/123",
        "location": {"name": "San Francisco, CA | New York, NY"},
        "offices": [{"location": "San Francisco, California, United States"}, {"location": None}],
        "metadata": [{"name": "Team", "value": ["Research"]}, {"name": "Location Type", "value": "On-Site"}],
        "content": "&lt;p&gt;Train &amp;amp; evaluate agents.&lt;/p&gt;&lt;script&gt;alert(1)&lt;/script&gt;",
        "first_published": "2026-10-19T11:17:24-04:00",
    }
    return job | changes


def lever_job(**changes):
    job = {
        "id": "0a1b-2c3d",
        "text": "Machine Learning Intern",
        "hostedUrl": "https://jobs.lever.co/globex/0a1b-2c3d",
        "categories": {"commitment": "Internship", "location": "Austin, TX", "allLocations": ["Austin, TX", "Remote"]},
        "country": "US",
        "workplaceType": "hybrid",
        "description": "<div>About Globex</div>",
        "lists": [{"text": "What you'll do", "content": "<li>Build models</li><li>Ship them</li>"}],
        "additional": "<p>Equal opportunity employer.</p>",
        "salaryRange": {"min": 50000, "max": 60000, "currency": "USD", "interval": "per-year-salary"},
        "createdAt": 1792506468000,
    }
    return job | changes


def ashby_job(**changes):
    job = {
        "id": "9f8e-7d6c",
        "title": "Applied AI Engineer",
        "jobUrl": "https://jobs.ashbyhq.com/initech/9f8e-7d6c",
        "location": "Seattle",
        "secondaryLocations": [{"location": "London", "address": {"postalAddress": {"addressCountry": "UK"}}}],
        "address": {"postalAddress": {"addressCountry": "United States"}},
        "workplaceType": "OnSite",
        "isRemote": False,
        "isListed": True,
        "employmentType": "FullTime",
        "descriptionHtml": "<p>Build <b>LLM</b> tools.</p>",
        "compensation": {"scrapeableCompensationSalarySummary": "$150K - $200K"},
        "publishedAt": "2026-07-30T21:04:56.462+00:00",
    }
    return job | changes


def test_api_addresses():
    assert greenhouse.api_url("acme") == "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
    assert lever.api_url("globex") == "https://api.lever.co/v0/postings/globex?mode=json"
    assert ashby.api_url("initech") == "https://api.ashbyhq.com/posting-api/job-board/initech?includeCompensation=true"


def test_greenhouse_job():
    jobs, skipped = greenhouse.parse({"jobs": [greenhouse_job()]}, "acme")
    job = jobs[0]
    assert (skipped, job.source, job.source_job_id, job.company) == (0, "greenhouse:acme", "123", "Acme")
    assert (job.locations, job.countries) == (["San Francisco, CA", "New York, NY"], ["United States"])
    assert (job.remote, job.date_posted, job.salary) == ("on-site", "2026-10-19", None)
    assert job.description == "Train & evaluate agents."  # the HTML arrives escaped; the script is dropped


def test_lever_job():
    job = lever.parse([lever_job()], "globex")[0][0]
    assert (job.source, job.source_job_id, job.company, job.title) == (
        "lever:globex",
        "0a1b-2c3d",
        "globex",
        "Machine Learning Intern",
    )
    assert (job.locations, job.countries, job.remote, job.level) == (
        ["Austin, TX", "Remote"],
        ["US"],
        "hybrid",
        "intern",
    )
    assert job.description == (
        "About Globex\n\nWhat you'll do\n\n- Build models\n\n- Ship them\n\nEqual opportunity employer."
    )
    assert (job.salary, job.date_posted) == ("USD 50,000–60,000 per year", "2026-10-20")


def test_ashby_job():
    job = ashby.parse({"jobs": [ashby_job()]}, "initech")[0][0]
    assert (job.source, job.company, job.locations, job.countries) == (
        "ashby:initech",
        "initech",
        ["Seattle", "London"],
        ["United States", "UK"],
    )
    assert (job.remote, job.level, job.description) == ("on-site", None, "Build LLM tools.")
    assert (job.salary, job.date_posted) == ("$150K - $200K", "2026-07-30")


def test_ashby_unlisted_jobs_are_left_out_and_is_remote_is_used():
    jobs, skipped = ashby.parse(
        {"jobs": [ashby_job(isListed=False), ashby_job(id="2", workplaceType=None, isRemote=True)]}, "initech"
    )
    assert ([job.source_job_id for job in jobs], skipped) == (["2"], 0)
    assert jobs[0].remote == "remote"


@pytest.mark.parametrize(
    ("module", "wrap", "make"),
    [
        (greenhouse, lambda jobs: {"jobs": jobs}, greenhouse_job),
        (lever, lambda jobs: jobs, lever_job),
        (ashby, lambda jobs: {"jobs": jobs}, ashby_job),
    ],
)
def test_broken_entries_are_skipped_and_counted(module, wrap, make):
    good = make()
    broken = [{key: value for key, value in good.items() if key not in ("title", "text")}, make(id=None), "junk"]
    jobs, skipped = module.parse(wrap([good, *broken]), "acme")
    assert (len(jobs), skipped) == (1, 3)


@pytest.mark.parametrize("module", [greenhouse, lever, ashby])
def test_wrong_shape_fails_the_board(module):
    for data in (None, "<html>", {"ok": False}, [[]] if module is not lever else {"jobs": []}):
        with pytest.raises(SourceError, match="unexpected format"):
            module.parse(data, "acme")


@pytest.mark.parametrize(("module", "empty"), [(greenhouse, {"jobs": []}), (lever, []), (ashby, {"jobs": []})])
def test_empty_board_means_no_openings(module, empty):
    assert module.parse(empty, "acme") == ([], 0)


def test_optional_fields_in_odd_shapes_do_not_lose_the_job():
    odd = {"offices": "Remote", "metadata": None, "content": None, "first_published": "yesterday", "location": {}}
    job = greenhouse.parse({"jobs": [greenhouse_job(**odd)]}, "acme")[0][0]
    assert (job.locations, job.countries, job.remote, job.description, job.date_posted) == ([], [], None, None, None)
    job = lever.parse([lever_job(salaryRange={"min": "lots"}, lists=None, country=None, createdAt=None)], "globex")[0][
        0
    ]
    assert (job.salary, job.countries, job.date_posted) == (None, [], None)
