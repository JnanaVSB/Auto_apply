"""Dashboard actions: not interested, fix location, add job, add company. Fake web and made-up jobs only."""

import threading
from contextlib import closing

import httpx
import pytest
from fastapi.testclient import TestClient
from test_boards import lever_job
from test_collect import ACME_JOB, FakeWeb, board_job
from web_helpers import ADDRESS, add_job, csrf, log_in

from app import collect, jobs
from app.config import Config, RoleSettings
from app.fetch import Fetcher
from app.sources import ashby, greenhouse, lever
from app.web import actions, main, security

pytestmark = pytest.mark.usefixtures("no_waiting")  # downloads never really wait


@pytest.fixture
def pages():
    return {}


@pytest.fixture
def web_down(pages):
    """Every job site stops answering."""
    pages[greenhouse.api_url("acme")] = httpx.ConnectError("no route to host")


@pytest.fixture
def client(data_dir, clock, pages, monkeypatch):
    """A logged-in dashboard whose web requests go to `pages` (a fake web), looking for ML roles."""
    roles = RoleSettings(keywords=["ML", "machine learning"])
    monkeypatch.setattr(actions, "load_config", lambda: Config(data_dir=data_dir, roles=roles))
    app = main.create_app(data_dir, security.Logins(clock=clock), lambda: Fetcher(httpx.MockTransport(FakeWeb(pages))))
    with TestClient(app, base_url=ADDRESS) as client:
        log_in(client)
        yield client


def post(client, address, **form):
    return client.post(address, data=form | {"csrf_token": csrf(client)}, follow_redirects=False)


def row(conn, sql, *values):
    return conn.execute(sql, values).fetchone()


def test_not_interested_hides_and_undo_shows_again(client, conn):
    with conn:
        job_id = add_job(conn, "ML Intern")
    for _ in range(2):  # clicking twice gives the same result
        response = post(client, f"/jobs/{job_id}/not-interested", value="1", back="/?f=1&level=mid")
        assert response.headers["location"] == f"/jobs/{job_id}?back=%2F%3Ff%3D1%26level%3Dmid"
    assert row(conn, "SELECT not_interested FROM jobs") == (1,)
    assert "ML Intern" not in client.get("/").text
    post(client, f"/jobs/{job_id}/not-interested", value="0")
    assert row(conn, "SELECT not_interested, count(*) FROM jobs") == (0, 1)  # never deleted


def test_a_location_you_set_stays_until_you_let_collections_decide(client, conn):
    board = jobs.Roles.from_settings([], ["ML"])
    job = collect.Job(source="greenhouse:acme", source_job_id="123", company="Acme", title="ML Intern",
                      url=ACME_JOB, locations=["San Francisco, CA"])  # fmt: skip
    jobs.save(conn, "greenhouse:acme", [job], board, "2026-10-20T10:00:00+00:00")
    job_id = row(conn, "SELECT id FROM jobs")[0]
    with conn:
        conn.execute("UPDATE jobs SET check_location = 1")
    post(client, f"/jobs/{job_id}/location", choice="no")
    assert row(conn, "SELECT us_location, check_location, location_by_you FROM jobs") == ("no", 0, 1)
    jobs.save(conn, "greenhouse:acme", [job], board, "2026-10-21T10:00:00+00:00")  # the board still says US
    assert row(conn, "SELECT us_location FROM jobs") == ("no",)
    post(client, f"/jobs/{job_id}/location", choice="auto")
    jobs.save(conn, "greenhouse:acme", [job], board, "2026-10-22T10:00:00+00:00")
    assert row(conn, "SELECT us_location, location_by_you FROM jobs") == ("yes", 0)


def test_location_choices_are_checked_and_a_missing_job_is_not_found(client, conn):
    with conn:
        job_id = add_job(conn, "ML Intern")
    assert post(client, f"/jobs/{job_id}/location", choice="maybe").status_code == 400
    assert post(client, f"/jobs/{job_id + 1}/location", choice="yes").status_code == 404
    assert post(client, f"/jobs/{job_id + 1}/not-interested", value="1").status_code == 404
    huge = 99999999999999999999  # more than the database can hold
    assert post(client, f"/jobs/{huge}/location", choice="yes").status_code == 404
    assert post(client, f"/jobs/{huge}/not-interested", value="1").status_code == 404


def test_add_a_board_job_by_its_link(client, conn, pages):
    pages[greenhouse.api_url("acme")] = {"jobs": [board_job(title="Accountant")]}
    text = post(client, "/add/job", link=ACME_JOB + "?gh_src=x").text
    job_id = row(conn, "SELECT id FROM jobs WHERE added_by_you = 1")[0]
    assert "Added: Acme, Accountant" in text and f'href="/jobs/{job_id}">Open it' in text
    assert "Already saved: Acme, Accountant" in post(client, "/add/job", link=ACME_JOB).text


@pytest.mark.parametrize(
    ("form", "message"),
    [
        ({"link": "https://initech.example/42", "company": "Initech", "title": "ML Engineer"}, "Added: Initech"),
        ({"link": "https://initech.example/42"}, "Both the company and the job title are needed."),
        ({"link": "https://simplify.jobs/p/123"}, "This is a simplify.jobs link, which the app must not open."),
        ({"link": ACME_JOB}, "The job&#39;s board could not be read (not found (404))."),
    ],
)
def test_add_job_uses_the_same_rules_as_collect_add(client, form, message):
    assert message in post(client, "/add/job", **form).text


@pytest.mark.parametrize(
    ("company", "names"),
    [
        ("Acme AI", ["acmeai", "acme-ai"]),
        ("Acme, Inc.", ["acmeinc", "acme-inc", "acme"]),
        ("Société Générale Co", ["societegeneraleco", "societe-generale-co", "societegenerale"]),
        ("Co", ["co"]),
        ("!!!", []),
    ],
)
def test_board_names_for_a_company(company, names):
    assert actions.board_names(company) == names


def test_find_company_shows_each_board_and_saves_nothing_until_add(client, conn, pages):
    pages[greenhouse.api_url("acme-ai")] = {"jobs": [board_job(company_name="Acme AI Inc."), board_job(
        id=124, title="Accountant", location={"name": "London, UK"})]}  # fmt: skip
    pages[lever.api_url("acmeai")] = [lever_job()]
    text = post(client, "/add/company", name="Acme AI").text
    assert (
        "<strong>greenhouse:acme-ai</strong>:\n      the board says the company is <strong>Acme AI Inc.</strong>"
        in text
    )
    assert "2 jobs listed, 1 match your roles." in text and "First jobs: ML Intern; Accountant" in text
    assert 'href="https://job-boards.greenhouse.io/acme-ai" target="_blank" rel="noopener noreferrer"' in text
    assert "Lever boards do not report a company name" in text
    assert row(conn, "SELECT count(*) FROM sources") == (0,) and row(conn, "SELECT count(*) FROM jobs") == (0,)
    text = post(client, "/add/board", board="greenhouse:acme-ai").text
    assert "Added: greenhouse:acme-ai (2 jobs on the board, 1 match your roles and are saved)." in text
    assert row(conn, "SELECT count(*) FROM jobs") == (1,)
    again = post(client, "/add/company", name="greenhouse:acme-ai").text
    assert "Already read on every collection." in again and "Stop reading this board" in again


def test_add_company_with_one_exact_board_tries_only_that(client, pages):
    pages[ashby.api_url("acme")] = {"jobs": []}
    text = post(client, "/add/company", name="ashby:acme").text
    assert "ashby:acme exists but lists no jobs right now, so it cannot be added." in text
    assert "No Greenhouse" not in text


def test_adding_a_board_brings_a_retired_or_stopped_board_back(client, conn, pages):
    with conn:
        conn.execute("INSERT INTO sources (name, misses, retired_on, stopped_on) VALUES ('greenhouse:acme', 3, "
                     "'2026-10-01', '2026-10-02')")  # fmt: skip
    pages[greenhouse.api_url("acme")] = {"jobs": [board_job()]}
    assert "Add and read it again" in post(client, "/add/company", name="greenhouse:acme").text
    assert "Added: greenhouse:acme" in post(client, "/add/board", board="greenhouse:acme").text
    assert row(conn, "SELECT misses, retired_on, stopped_on FROM sources") == (0, None, None)


@pytest.mark.parametrize(("board", "message"), [("acme", "not a board name"), ("greenhouse:acme", "was not found")])
def test_add_board_refuses_odd_names_and_missing_boards(client, conn, board, message):
    assert message in post(client, "/add/board", board=board).text
    assert row(conn, "SELECT count(*) FROM sources") == (0,)


def test_stop_reading_a_board_and_read_it_again(client, conn):
    with conn:
        job_id = add_job(conn, "ML Intern")
        conn.execute("INSERT INTO sources (name) VALUES ('greenhouse:acme')")
    assert "Stop reading this board" in client.get(f"/jobs/{job_id}").text
    response = post(client, "/boards/stop", board="greenhouse:acme", value="1", job=str(job_id))
    assert response.headers["location"].startswith(f"/jobs/{job_id}?back=")
    assert row(conn, "SELECT stopped_on IS NOT NULL FROM sources") == (1,)
    page = client.get(f"/jobs/{job_id}").text
    assert "stopped by you on" in page and "Read this board again" in page
    assert post(client, "/boards/stop", board="greenhouse:acme", value="0").headers["location"] == "/settings"
    assert row(conn, "SELECT stopped_on, count(*) FROM jobs, sources") == (None, 1)  # nothing deleted


@pytest.mark.parametrize("board", ["greenhouse:unknown", "simplify:Summer2027-Internships"])
def test_only_known_company_boards_can_be_stopped(client, conn, board):
    with conn:
        conn.execute("INSERT INTO sources (name) VALUES ('simplify:Summer2027-Internships')")
    assert post(client, "/boards/stop", board=board, value="1").status_code == 404


def test_add_company_not_found_gives_the_tip(client, conn):
    text = post(client, "/add/company", name="Acme AI").text
    assert "No Greenhouse, Lever or Ashby board found for &#39;Acme AI&#39; (tried acmeai, acme-ai)." in text
    assert "type the name from that address" in text
    assert row(conn, "SELECT count(*) FROM sources") == (0,)


def test_add_company_says_when_a_site_could_not_be_read(client, pages):
    pages[greenhouse.api_url("acmeai")] = 500  # busy or failing, every retry
    text = post(client, "/add/company", name="Acme AI").text
    assert "Greenhouse could not be read (busy or failing (status 500); gave up after 3 retries)." in text
    assert "No Greenhouse, Lever or Ashby board found" in text  # Lever and Ashby really said "not found"


@pytest.mark.parametrize("name", ["", "   ", "!!!"])
def test_add_company_needs_a_name(client, name):
    assert "Type a company name" in post(client, "/add/company", name=name).text


@pytest.mark.parametrize(
    ("address", "form"),
    [
        ("/jobs/1/not-interested", {"value": "1"}),
        ("/jobs/1/location", {"choice": "no"}),
        ("/add/job", {"link": ACME_JOB}),
        ("/add/company", {"name": "Acme"}),
        ("/add/board", {"board": "greenhouse:acme"}),
        ("/boards/stop", {"board": "greenhouse:acme", "value": "1"}),
    ],
)
def test_every_action_needs_the_anti_forgery_token(client, conn, address, form):
    with conn:
        add_job(conn, "ML Intern")
    response = client.post(address, data=form)
    assert response.status_code == 403
    assert row(conn, "SELECT not_interested, us_location, count(*) FROM jobs") == (0, "yes", 1)
    assert row(conn, "SELECT count(*) FROM sources") == (0,)


def test_job_page_shows_the_action_buttons(client, conn):
    with conn:
        job_id = add_job(conn, "ML Intern", location_by_you=1)
    text = client.get(f"/jobs/{job_id}").text
    assert 'value="1"><button type="submit">Not interested' in text and "Let collections decide again" in text
    assert text.count(f'name="csrf_token" value="{csrf(client)}"') >= 2


def test_add_page_opens(client):
    assert "Add a company" in client.get("/add").text


def test_job_link_lookup_cleans_the_link_like_saving(conn):
    with conn:
        job_id = add_job(conn, "ML Intern", apply_url=ACME_JOB)
    assert actions.queries.job_id_by_link(conn, f" {ACME_JOB}?gh_src=x ") == job_id
    assert actions.queries.job_id_by_link(conn, "https://other.example/1") is None


def test_add_actions_open_their_own_connection(data_dir, monkeypatch):
    with closing(Fetcher(httpx.MockTransport(FakeWeb({})))) as fetcher:
        message, job_id = actions.add_job(data_dir, fetcher, "https://initech.example/9", "Initech", "ML")
    assert (message, job_id) == ("Added: Initech, ML", 1)


def test_add_job_says_when_the_board_does_not_answer(client, web_down):
    text = post(client, "/add/job", link=ACME_JOB).text
    assert "The job&#39;s board could not be read (connection failed (ConnectError); gave up after 3 retries" in text
    assert "your network may be blocking it" in text


def test_a_double_click_on_add_job_finds_already_saved(data_dir, pages):
    pages[greenhouse.api_url("acme")] = {"jobs": [board_job()]}
    started, results = threading.Barrier(2), []

    def add():
        started.wait()  # both clicks arrive together
        with closing(Fetcher(httpx.MockTransport(FakeWeb(pages)))) as fetcher:
            results.append(actions.add_job(data_dir, fetcher, ACME_JOB, "", "")[0])

    clicks = [threading.Thread(target=add) for _ in range(2)]
    for click in clicks:
        click.start()
    for click in clicks:
        click.join(10)
    assert sorted(results) == ["Added: Acme, ML Intern", "Already saved: Acme, ML Intern"]
