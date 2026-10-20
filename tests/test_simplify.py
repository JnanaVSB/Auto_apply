"""Simplify lists, tested with small made-up entries in the real file's shape (fake companies only)."""

import pytest

from app.sources import SourceError, simplify

LIST_URL = "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json"
SOURCE = "simplify:Summer2027-Internships"


def entry(**changes):
    job = {
        "id": "id-1",
        "title": "ML Intern",
        "company_name": "Acme",
        "url": "https://job-boards.greenhouse.io/acme/jobs/123",
        "locations": ["San Francisco, CA"],
        "category": "AI/ML/Data",
        "sponsorship": "Other",
        "terms": ["Summer 2027"],
        "date_posted": 1792506468,
        "active": True,
        "is_visible": True,
    }
    return job | changes


def test_source_name_comes_from_the_repository():
    assert simplify.source_name(LIST_URL) == SOURCE


def test_entry_becomes_a_job():
    jobs, skipped = simplify.parse([entry()], SOURCE, "intern")
    assert skipped == 0
    job = jobs[0]
    assert (job.source, job.source_job_id, job.company, job.title) == (SOURCE, "id-1", "Acme", "ML Intern")
    assert (job.level, job.category, job.sponsorship, job.terms) == ("intern", "AI/ML/Data", "unknown", ["Summer 2027"])
    assert (job.date_posted, job.listed, job.description) == ("2026-10-20", True, None)


@pytest.mark.parametrize(
    ("wording", "expected"),
    [
        ("Offers Sponsorship", "offers"),
        ("Does Not Offer Sponsorship", "no"),
        ("U.S. Citizenship is Required", "citizens only"),
        ("Other", "unknown"),
        (None, "unknown"),
    ],
)
def test_sponsorship_wording(wording, expected):
    assert simplify.parse([entry(sponsorship=wording)], SOURCE, "intern")[0][0].sponsorship == expected


def test_milliseconds_date_and_inactive_entry():
    job = simplify.parse([entry(date_posted=1792506468000, active=False)], SOURCE, "new grad")[0][0]
    assert (job.date_posted, job.listed, job.level) == ("2026-10-20", False, "new grad")


def test_hidden_entries_are_ignored_and_broken_ones_counted():
    data = [
        entry(),
        entry(id="hidden", is_visible=False),
        entry(title=""),
        entry(locations="San Francisco, CA"),
        entry(terms="Summer 2027"),
        {k: v for k, v in entry().items() if k != "url"},
        "not an entry",
    ]
    jobs, skipped = simplify.parse(data, SOURCE, "intern")
    assert [job.source_job_id for job in jobs] == ["id-1"]
    assert skipped == 5


@pytest.mark.parametrize("data", [{"jobs": []}, [], [entry(title=None)], None])
def test_unusable_list_fails_the_source(data):
    with pytest.raises(SourceError):
        simplify.parse(data, SOURCE, "intern")


def test_boards_come_from_active_jobs_only():
    data = [
        entry(id="1", url="https://job-boards.greenhouse.io/acme/jobs/1"),
        entry(id="2", url="https://jobs.lever.co/Globex/0a1b"),
        entry(id="3", url="https://jobs.ashbyhq.com/initech/9f8e", active=False),
        entry(id="4", url="https://acme.wd1.myworkdayjobs.com/jobs/4"),
        entry(id="5", url="https://simplify.jobs/p/5"),
    ]
    jobs, _ = simplify.parse(data, SOURCE, "intern")
    assert simplify.linked_boards(jobs) == {"greenhouse:acme", "lever:globex"}
