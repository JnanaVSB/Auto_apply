"""The collect and add commands, end to end, with a fake web and a throwaway encrypted database."""

import json
import time
from contextlib import closing

import httpx
import pytest
import sqlcipher3

from app import collect, db, fetch, jobs
from app.config import Config, RoleSettings, SimplifyList, SourceSettings

LIST = "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json"
ACME = "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
GLOBEX = "https://api.lever.co/v0/postings/globex?mode=json"
ACME_JOB = "https://job-boards.greenhouse.io/acme/jobs/123"
PASSWORD = "fake-master-password-1"  # noqa: S105 (fake value, tests only)


def entry(**changes):
    job = {"id": "s-1", "title": "ML Intern", "company_name": "Acme", "url": ACME_JOB, "locations": ["SF"]}
    return job | {"category": "AI/ML/Data", "terms": ["Summer 2027"], "active": True} | changes


def board_job(**changes):
    job = {"id": 123, "title": "ML Intern", "company_name": "Acme", "absolute_url": ACME_JOB}
    job["location"] = {"name": "San Francisco, CA"}
    return job | {"content": "&lt;p&gt;Train models.&lt;/p&gt;"} | changes


class FakeWeb:
    """Answers each address from `pages`: JSON, (JSON, version tag), a status code or an error to raise."""

    def __init__(self, pages):
        self.pages = pages
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        page = self.pages.get(str(request.url), 404)
        if isinstance(page, BaseException):
            raise page
        if isinstance(page, int):
            return httpx.Response(page)
        data, etag = page if isinstance(page, tuple) else (page, None)
        if etag and request.headers.get("If-None-Match") == etag:
            return httpx.Response(304)
        return httpx.Response(200, json=data, headers={"ETag": etag} if etag else {})


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    class Clock:
        now = 0.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    monkeypatch.setattr(fetch, "time", Clock())


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "RECOVERY_KDF_ITER", 1_000)
    db.setup(tmp_path / "data", PASSWORD)
    return tmp_path / "data"


@pytest.fixture
def conn(data_dir):
    with closing(db.connect(data_dir)) as connection:
        yield connection


def settings(data_dir, extra_boards=()):
    return Config(
        data_dir=data_dir,
        roles=RoleSettings(simplify_categories=["AI/ML/Data"], keywords=["ML"]),
        sources=SourceSettings(
            simplify_lists=[SimplifyList(url=LIST, level="intern")], extra_boards=list(extra_boards)
        ),
    )


def run(conn, data_dir, pages, extra_boards=()):
    """One collection against a fake web; returns the printed lines and the web's record of requests."""
    web, lines = FakeWeb(pages), []
    with closing(fetch.Fetcher(httpx.MockTransport(web))) as fetcher:
        collect.collect(conn, settings(data_dir, extra_boards), fetcher, lines.append)
    return lines, web


def one(conn, sql, *values):
    return conn.execute(sql, values).fetchone()


PAGES = {
    LIST: ([entry(), entry(id="s-2", title="Accountant", category="Finance", url="https://acme.example/2")], '"v1"'),
    ACME: {"jobs": [board_job()]},
}


def test_full_run_saves_matching_jobs_and_prints_a_summary(conn, data_dir):
    lines, _ = run(conn, data_dir, PAGES | {GLOBEX: []}, extra_boards=["lever:globex"])
    assert lines == [
        "Simplify Summer2027-Internships: 1 matching (1 new, 0 updated, 0 closed)",
        "Reading 2 company boards one at a time (about 1 minutes)...",
        "Company boards: 2 of 2 read, 1 matching (0 new, 1 updated, 0 closed)",
        "Total: 1 new, 1 updated, 0 closed.",
        "No sources failed.",
    ]
    assert one(conn, "SELECT count(*), max(description), max(status) FROM jobs") == (1, "Train models.", "open")
    assert one(conn, "SELECT etag FROM sources WHERE name = 'simplify:Summer2027-Internships'") == ('"v1"',)
    assert one(conn, "SELECT outcome, new_jobs, updated_jobs, failures FROM collect_runs") == ("done", 1, 1, "[]")


def test_second_run_creates_no_duplicates_and_skips_an_unchanged_list(conn, data_dir):
    run(conn, data_dir, PAGES)
    lines, web = run(conn, data_dir, PAGES)
    assert lines[0] == "Simplify Summer2027-Internships: unchanged since the last run"
    assert lines[2] == "Company boards: 1 of 1 read, 1 matching (0 new, 0 updated, 0 closed)"
    assert web.requests[0].headers["If-None-Match"] == '"v1"'
    assert one(conn, "SELECT count(*) FROM jobs") == (1,)


def test_failed_sources_close_nothing_and_are_named(conn, data_dir):
    run(conn, data_dir, PAGES)
    lines, _ = run(conn, data_dir, {LIST: 500, ACME: 404})
    assert lines[-3:] == [
        "2 sources failed:",
        "  - Simplify Summer2027-Internships: busy or failing (status 500); gave up after 3 retries",
        "  - Greenhouse board 'acme': not found (404)",
    ]
    assert one(conn, "SELECT status FROM jobs") == ("open",)
    assert len(json.loads(one(conn, "SELECT failures FROM collect_runs ORDER BY id DESC")[0])) == 2


def test_version_tag_is_kept_only_after_a_successful_save(conn, data_dir, monkeypatch):
    def disk_full(*args, **kwargs):
        raise sqlcipher3.OperationalError("database or disk is full")

    with monkeypatch.context() as broken:
        broken.setattr(jobs, "save", disk_full)
        with pytest.raises(sqlcipher3.OperationalError):
            run(conn, data_dir, PAGES)
    assert one(conn, "SELECT count(*) FROM sources") == (0,)
    assert one(conn, "SELECT outcome FROM collect_runs") == ("stopped",)
    _, web = run(conn, data_dir, PAGES)
    assert "If-None-Match" not in web.requests[0].headers  # downloaded afresh


def test_boards_dropped_by_simplify_are_still_read(conn, data_dir):
    run(conn, data_dir, PAGES)
    _, web = run(conn, data_dir, {LIST: ([entry(url="https://acme.example/jobs/1")], '"v2"'), ACME: {"jobs": []}})
    assert ACME in [str(request.url) for request in web.requests]
    assert one(conn, "SELECT status FROM jobs WHERE apply_url = ?", ACME_JOB) == ("closed",)


def test_cancelling_keeps_finished_sources_and_is_recorded(conn, data_dir):
    with pytest.raises(KeyboardInterrupt):
        run(conn, data_dir, PAGES | {ACME: KeyboardInterrupt()})
    assert one(conn, "SELECT count(*) FROM jobs") == (1,)  # the Simplify list was saved before stopping
    assert one(conn, "SELECT outcome FROM collect_runs") == ("cancelled",)


def test_a_second_copy_is_refused(tmp_path):
    with closing(collect._lock(tmp_path)):
        with pytest.raises(collect.CollectError, match="already running"):
            collect._lock(tmp_path)
    collect._lock(tmp_path).close()  # free again once the first run has finished


def test_add_a_board_job_ignores_filters_and_remembers_the_board(conn):
    web = FakeWeb({ACME: {"jobs": [board_job(title="Accountant", location={"name": "London, UK"})]}})
    with closing(fetch.Fetcher(httpx.MockTransport(web))) as fetcher:
        assert collect.add(conn, fetcher, ACME_JOB + "?gh_src=x") == "Added: Acme, Accountant"
        assert collect.add(conn, fetcher, ACME_JOB) == "Already saved: Acme, Accountant"
    assert one(conn, "SELECT us_location, description FROM jobs") == ("unclear", "Train models.")
    assert one(conn, "SELECT name FROM sources") == ("greenhouse:acme",)


def test_add_another_site_asks_for_company_and_title_once(conn):
    answers = iter(["Initech", "ML Engineer"])
    link = "https://initech.example/careers/42?utm_source=x"
    assert collect.add(conn, None, link, ask=lambda prompt: next(answers)) == "Added: Initech, ML Engineer"
    assert collect.add(conn, None, link, ask=lambda prompt: pytest.fail("asked again")) == (
        "Already saved: Initech, ML Engineer"
    )
    assert one(conn, "SELECT apply_url, description FROM jobs") == ("https://initech.example/careers/42", None)


@pytest.mark.parametrize(
    ("link", "message"),
    [
        ("https://simplify.jobs/p/123", "must not open"),
        ("javascript:alert(1)", "not a web link"),
        ("https://someone@initech.example/1", "cannot be used"),
    ],
)
def test_add_refuses_links_it_must_not_use(conn, link, message):
    answers = iter(["Initech", "ML Engineer"])
    with pytest.raises(collect.CollectError, match=message):
        collect.add(conn, None, link, ask=lambda prompt: next(answers))
    assert one(conn, "SELECT count(*) FROM jobs") == (0,)


def test_add_reports_a_job_gone_from_its_board_and_missing_answers(conn):
    with closing(fetch.Fetcher(httpx.MockTransport(FakeWeb({ACME: {"jobs": []}})))) as fetcher:
        with pytest.raises(collect.CollectError, match="no longer on the Greenhouse board 'acme'"):
            collect.add(conn, fetcher, ACME_JOB)
    with pytest.raises(collect.CollectError, match="Both the company and the job title"):
        collect.add(conn, None, "https://initech.example/1", ask=lambda prompt: " ")


@pytest.fixture
def main(monkeypatch, capsys):
    def main(argv, config):
        monkeypatch.setattr(collect, "load_config", lambda: config)
        return collect.main(argv), capsys.readouterr().err

    return main


def test_missing_settings_and_database_are_explained(main, data_dir, tmp_path):
    assert main([], Config(data_dir=data_dir)) == (
        1,
        "Error: No roles set in data/config.yaml. Copy the 'roles' section from config.example.yaml.\n",
    )
    code, err = main([], settings(tmp_path / "nothing-here"))
    assert code == 1
    assert "app.db not found" in err
    assert "python -m app.db setup" in err


def test_cancel_messages(main, data_dir, monkeypatch):
    def cancel(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(collect, "collect", cancel)
    assert main([], settings(data_dir))[1] == (
        "\nCancelled. Sources finished before you stopped are saved; run it again to continue.\n"
    )
    monkeypatch.setattr(collect, "add", cancel)
    assert main(["add", ACME_JOB], settings(data_dir))[1] == "\nCancelled.\n"


def test_database_and_file_problems_give_one_line_messages(main, data_dir, monkeypatch):
    def locked(*args, **kwargs):
        raise sqlcipher3.OperationalError("database is locked")

    def no_permission(*args, **kwargs):
        raise PermissionError(13, "Permission denied", "/home/someone/private/data/app.db")

    monkeypatch.setattr(collect, "collect", locked)
    assert main([], settings(data_dir)) == (
        1,
        "Error: the database could not be read or written (database is locked).\n",
    )
    monkeypatch.setattr(collect, "collect", no_permission)
    assert main([], settings(data_dir)) == (1, "Error: Permission denied: app.db\n")  # never the full path


def test_add_reports_an_unreachable_board(main, data_dir, monkeypatch):
    def offline(*args, **kwargs):
        raise fetch.FetchError("connection failed (ConnectError); gave up after 3 retries")

    monkeypatch.setattr(collect, "add", offline)
    assert main(["add", ACME_JOB], settings(data_dir)) == (
        1,
        "Error: the job's board could not be read (connection failed (ConnectError); gave up after 3 retries).\n",
    )


def board_state(conn, name="greenhouse:acme"):
    return one(conn, "SELECT misses, retired_on FROM sources WHERE name = ?", name)


def requested(web):
    return [str(request.url) for request in web.requests]


def test_a_board_not_found_three_runs_in_a_row_is_retired_and_named_once(conn, data_dir):
    pages = PAGES | {ACME: 404}
    summaries = [run(conn, data_dir, pages)[0] for _ in range(3)]
    stopped = [line for lines in summaries for line in lines if line.startswith("Stopped reading")]
    assert stopped == [
        "Stopped reading 1 boards that were not found 3 runs in a row "
        "(read again if Simplify links a newer job there, or if you add them to extra_boards):"
    ]
    assert summaries[2][-1] == "  - Greenhouse board 'acme'"
    assert board_state(conn)[0] == 3
    lines, web = run(conn, data_dir, pages)
    assert ACME not in requested(web)
    assert lines[-1] == "No sources failed."
    assert one(conn, "SELECT status FROM jobs") == ("open",)  # retiring closes nothing


def test_only_not_found_runs_in_a_row_count(conn, data_dir):
    for page in (404, 404, 500, {"jobs": [board_job()]}, 404, 404):  # a server error counts for nothing
        run(conn, data_dir, PAGES | {ACME: page})
    assert board_state(conn) == (2, None)


def test_a_retired_board_returns_when_simplify_links_a_newer_job(conn, data_dir):
    for _ in range(3):
        run(conn, data_dir, PAGES | {ACME: 404})
    older = entry(id="s-3", url="https://job-boards.greenhouse.io/acme/jobs/5", date_posted=1790000000)
    _, web = run(conn, data_dir, {LIST: ([entry(), older], '"v2"'), ACME: 404})
    assert ACME not in requested(web)  # still retired: nothing new posted there
    newer = entry(id="s-4", url="https://job-boards.greenhouse.io/acme/jobs/6", date_posted=int(time.time()) + 86400)
    _, web = run(conn, data_dir, {LIST: ([entry(), newer], '"v3"'), ACME: {"jobs": [board_job()]}})
    assert ACME in requested(web)
    assert board_state(conn) == (0, None)


def test_extra_boards_are_never_retired(conn, data_dir):
    for _ in range(4):
        lines, web = run(conn, data_dir, {GLOBEX: 404}, extra_boards=["lever:globex"])
    assert GLOBEX in requested(web)
    assert not any(line.startswith("Stopped reading") for line in lines)


def test_adding_a_job_brings_its_retired_board_back(conn, data_dir):
    for _ in range(3):
        run(conn, data_dir, PAGES | {ACME: 404})
    with closing(fetch.Fetcher(httpx.MockTransport(FakeWeb({ACME: {"jobs": [board_job()]}})))) as fetcher:
        collect.add(conn, fetcher, ACME_JOB)
    assert board_state(conn) == (0, None)
