"""The Tracker page: the six lanes, I applied, Move to, the round, Add an application, and the notices."""

from datetime import date

import pytest
from helpers import add_job, csrf, log_in, one
from test_tracker import version

from app import tracker

LINK = "https://careers.initech.example/jobs/7"


@pytest.fixture
def client(client):
    log_in(client)
    return client


@pytest.fixture
def ready(conn):
    """A made-up job with a finished resume version: a card in Ready."""
    with conn:
        job = add_job(conn, "Robotics Intern")
        version(conn, job)
    return job


def post(client, address, **form):
    return client.post(address, data=form | {"csrf_token": csrf(client)}, follow_redirects=False)


def test_the_page_shows_six_lanes_and_a_ready_card_and_the_menu_has_tracker(client, ready):
    text = client.get("/tracker").text
    assert all(f'id="lane-{stage}"' in text for stage in tracker.STAGES)
    assert f'id="job-{ready}" data-stage="ready"' in text and "I applied" in text
    assert 'href="/tracker"' in text and "Tracker" in text


def test_i_applied_moves_the_card_and_a_second_tab_is_told(client, conn, ready):
    reply = post(client, f"/tracker/{ready}/move", **{"from": "ready", "to": "applied"})
    assert (reply.status_code, reply.headers["location"]) == (303, f"/tracker?moved=applied#job-{ready}")
    assert tracker.stage(conn, ready) == "applied"
    assert "Moved to Applied." in client.get(reply.headers["location"]).text
    again = post(client, f"/tracker/{ready}/move", **{"from": "ready", "to": "applied"})
    assert again.headers["location"] == f"/tracker?notice=stale#job-{ready}"
    assert "Already moved in another tab" in client.get(again.headers["location"]).text


@pytest.mark.parametrize("start, to", [("ready", "ghosted"), ("applied", "applied"), ("", "applied")])
def test_an_unknown_or_unchanged_stage_is_refused(client, ready, start, to):
    assert post(client, f"/tracker/{ready}/move", **{"from": start, "to": to}).status_code == 400


@pytest.mark.parametrize("action", ["move", "round"])
def test_an_unknown_job_is_not_found(client, action):
    assert post(client, f"/tracker/999/{action}", **{"from": "ready", "to": "applied", "round": "2"}).status_code == 404


def test_a_move_without_the_page_s_token_is_refused(client, conn, ready):
    reply = client.post(f"/tracker/{ready}/move", data={"from": "ready", "to": "applied"}, follow_redirects=False)
    assert reply.status_code == 403 and tracker.stage(conn, ready) is None


def test_the_round_is_saved_only_on_an_interview_card(client, conn, ready):
    assert (
        post(client, f"/tracker/{ready}/round", round="2").headers["location"] == f"/tracker?notice=stale#job-{ready}"
    )
    tracker.move(conn, ready, "ready", "interview", date(2026, 11, 4))
    assert post(client, f"/tracker/{ready}/round", round="9").status_code == 400
    assert (
        post(client, f"/tracker/{ready}/round", round="2").headers["location"] == f"/tracker?notice=round#job-{ready}"
    )
    assert post(client, f"/tracker/{ready}/round", round="2").headers["location"] == f"/tracker#job-{ready}"
    assert 'value="2" selected' in client.get("/tracker").text


@pytest.mark.parametrize("query", ["moved=<script>alert(1)</script>", "notice=x", "moved=ghosted"])
def test_the_address_shows_only_fixed_notices(client, query):
    text = client.get(f"/tracker?{query}").text
    assert 'role="status"' not in text and "<script>alert" not in text


def test_a_company_name_is_shown_as_text(client, conn, ready):
    with conn:
        conn.execute("UPDATE jobs SET company = '<script>alert(1)</script>' WHERE id = ?", (ready,))
    text = client.get("/tracker").text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text and "<script>alert(1)" not in text


def test_add_an_application_puts_it_in_applied(client, conn):
    form = {"link": LINK, "company": "Initech", "role": "ML Engineer", "applied_on": "2026-11-01"}
    text = post(client, "/tracker/add", **form).text
    job = one(conn, "SELECT id FROM jobs WHERE apply_url = ?", LINK)[0]
    assert "Added to Applied." in text and f'id="job-{job}" data-stage="applied"' in text


@pytest.mark.parametrize("applied_on", ["2999-01-01", "not a date", ""])
def test_add_with_a_bad_date_saves_nothing_and_keeps_what_was_typed(client, conn, applied_on):
    form = {"link": LINK, "company": "Initech", "role": "ML Engineer", "applied_on": applied_on}
    reply = post(client, "/tracker/add", **form)
    assert reply.status_code == 400 and tracker.BAD_DATE in reply.text
    assert 'value="Initech"' in reply.text and f'value="{LINK}"' in reply.text
    assert one(conn, "SELECT count(*) FROM jobs")[0] == 0


def test_show_all_lists_a_long_lane_whole_and_ignores_an_unknown_lane(client, conn):
    with conn:
        for _ in range(tracker.LANE_CARDS + 1):
            job = add_job(conn, "Robotics Intern")
            conn.execute(
                "INSERT INTO tracker (job_id, stage, applied_on, changed_on) VALUES (?, 'applied', '2026-11-01', ?)",
                (job, tracker.now()),
            )
    assert client.get("/tracker").text.count('data-stage="applied"') == 30
    assert "Show all 31" in client.get("/tracker?all=offer").text
    whole = client.get("/tracker?all=applied").text
    assert whole.count('data-stage="applied"') == 31 and "Show all" not in whole


def test_the_job_page_goes_back_to_the_tracker(client, ready):
    assert '<a href="/tracker">Back to the list</a>' in client.get(f"/jobs/{ready}?back=/tracker").text
