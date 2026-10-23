"""Plain helpers shared by the tests (fixtures live in conftest.py): fake values, a fake web, made-up jobs, login."""

from itertools import count

import httpx

from app.config import Config, RoleSettings, SimplifyList, SourceSettings
from app.web import security

PASSWORD = "fake dashboard words here"  # noqa: S105 (fake value, tests only)
MASTER_PASSWORD = "fake-master-password-1"  # noqa: S105 (fake value, tests only)
ADDRESS = "http://127.0.0.1:8765"
LIST = "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json"
ACME = "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
ACME_JOB = "https://job-boards.greenhouse.io/acme/jobs/123"
URLS = count()


class Clock:
    """A clock the test moves by hand."""

    now = 1_000.0

    def __call__(self):
        return self.now


def log_in(client, password=PASSWORD):
    return client.post("/login", data={"password": password}, follow_redirects=False)


def csrf(client):
    return client.app.state.logins.sessions[client.cookies[security.COOKIE]].csrf


def add_job(conn, title, sources=("greenhouse:acme",), **values):
    """A made-up job, stored directly, listed by `sources`."""
    row = {
        "apply_url": f"https://jobs.example/{next(URLS)}", "match_key": title, "company": "Acme", "title": title,
        "locations": '["Austin, TX"]', "us_location": "yes", "remote": "unknown", "level": "intern",
        "sponsorship": "unknown", "terms": "[]", "first_seen": "2026-10-01", "last_seen": "2026-10-01",
        "status": "open",
    } | values  # fmt: skip
    job_id = conn.execute(
        f"INSERT INTO jobs ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",  # noqa: S608 (test helper)
        list(row.values()),
    ).lastrowid
    for source in sources:
        conn.execute("INSERT INTO job_sources (source, source_job_id, job_id, listed) VALUES (?, ?, ?, 1)",
                     (source, str(job_id), job_id))  # fmt: skip
    return job_id


def one(conn, sql, *values):
    return conn.execute(sql, values).fetchone()


def entry(**changes):
    job = {"id": "s-1", "title": "ML Intern", "company_name": "Acme", "url": ACME_JOB, "locations": ["SF"]}
    return job | {"category": "AI/ML/Data", "terms": ["Summer 2027"], "active": True} | changes


def board_job(**changes):
    job = {"id": 123, "title": "ML Intern", "company_name": "Acme", "absolute_url": ACME_JOB}
    job["location"] = {"name": "San Francisco, CA"}
    return job | {"content": "&lt;p&gt;Train models.&lt;/p&gt;"} | changes


class FakeWeb:
    """Answers each address from `pages`: JSON, (JSON, version tag), a status code, a reply or an error to raise.

    Sites in `down` never answer at all; sites in `silent` answer the quick reachability check, then go quiet.
    """

    def __init__(self, pages, down=(), silent=()):
        self.pages, self.down, self.silent = pages, set(down), set(silent)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        host = request.url.host
        if host in self.down or (host in self.silent and request.method == "GET"):
            raise httpx.ConnectError("no route to host")
        page = self.pages.get(str(request.url), 404)
        if isinstance(page, BaseException):
            raise page
        if isinstance(page, int):
            return httpx.Response(page)
        if isinstance(page, httpx.Response):
            return page
        data, etag = page if isinstance(page, tuple) else (page, None)
        if etag and request.headers.get("If-None-Match") == etag:
            return httpx.Response(304)
        return httpx.Response(200, json=data, headers={"ETag": etag} if etag else {})


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


def settings(data_dir, extra_boards=()):
    return Config(
        data_dir=data_dir,
        roles=RoleSettings(simplify_categories=["AI/ML/Data"], keywords=["ML"]),
        sources=SourceSettings(
            simplify_lists=[SimplifyList(url=LIST, level="intern")], extra_boards=list(extra_boards)
        ),
    )
