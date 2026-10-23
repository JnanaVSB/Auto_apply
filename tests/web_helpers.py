"""Plain helpers shared by the dashboard tests (fixtures live in conftest.py): fake values, login, made-up jobs."""

from itertools import count

from app.web import security

PASSWORD = "fake dashboard words here"  # noqa: S105 (fake value, tests only)
ADDRESS = "http://127.0.0.1:8765"
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
