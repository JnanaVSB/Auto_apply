"""Feeds in a collection: reading, closing, switching off. A fake web and a throwaway database, made-up jobs only."""

import json
from contextlib import closing
from datetime import date

import httpx
import pytest
import sqlcipher3
from helpers import FakeWeb, one, settings

from app import collect, fetch, jobs, tracker
from app.config import SourceSettings
from app.sources import remoteok

pytestmark = pytest.mark.usefixtures("no_waiting")  # downloads never really wait

FEED = "feed:remoteok"
NOTICE = {"last_updated": 1792506468, "legal": "Made-up terms notice."}
LINK = "https://remoteok.com/remote-jobs/remote-ml-engineer-acme-1"
OTHER_LINK = "https://remoteok.com/remote-jobs/remote-ml-intern-acme-2"
LEVER = "https://api.lever.co/v0/postings/acme?mode=json"
BOARD_JOB = {"id": "l-1", "text": "ML Engineer", "hostedUrl": "https://jobs.lever.co/acme/l-1",
             "categories": {"location": "Austin, TX"}}  # fmt: skip
OFF, ON = (), ("remoteok",)


def listing(url=LINK, title="ML Engineer"):
    return {"id": "1", "epoch": 1792506468, "company": "Acme", "position": title, "location": "Austin, TX",
            "description": "<p>Train models.</p>", "salary_min": 0, "salary_max": 0, "url": url}  # fmt: skip


def feed(*entries):
    return {remoteok.API: [NOTICE, *entries]}


def run(conn, data_dir, pages, feeds=ON, extra_boards=()):
    web, lines = FakeWeb(pages), []
    sources = SourceSettings(feeds=list(feeds), extra_boards=list(extra_boards))
    config = settings(data_dir).model_copy(update={"sources": sources})
    with closing(fetch.Fetcher(httpx.MockTransport(web))) as fetcher:
        collect.collect(conn, config, fetcher, lines.append)
    return lines, web


def asked(web):
    return [str(request.url) for request in web.requests]


def state(conn):
    return conn.execute("SELECT title, status, status_reason FROM jobs ORDER BY id").fetchall()


def entry(conn, source=FEED):
    return one(conn, "SELECT listed, unlisted_on, unlisted_reason FROM job_sources WHERE source = ?", source)


def test_a_feed_is_read_after_the_boards_and_says_so(conn, data_dir):
    lines, web = run(conn, data_dir, feed(listing()) | {LEVER: []}, extra_boards=["lever:acme"])
    assert asked(web).index(LEVER) < asked(web).index(remoteok.API)
    assert "Remote OK: 1 matching (1 new, 0 updated, 0 no longer available)" in lines
    assert state(conn) == [("ML Engineer", "open", None)]
    assert one(conn, "SELECT source_job_id FROM job_sources") == (LINK,)


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        (403, "refused (status 403)"),
        (500, "busy or failing (status 500); gave up after 3 retries"),
        (httpx.ReadTimeout("slow"), "timed out; gave up after 3 retries"),
        (httpx.Response(301, headers={"Location": "https://www.remoteok.com/api"}), "moved to another address"),
    ],
)
def test_a_feed_that_cannot_be_read_fails_alone_and_closes_nothing(conn, data_dir, reply, reason):
    run(conn, data_dir, feed(listing()))
    lines, _ = run(conn, data_dir, {remoteok.API: reply, LEVER: [BOARD_JOB]}, extra_boards=["lever:acme"])
    assert [line for line in lines if line.startswith("  - Remote OK: ")][0].startswith(f"  - Remote OK: {reason}")
    assert "1 source failed:" in lines
    assert ("ML Engineer", "open", None) in state(conn)
    assert one(conn, "SELECT count(*) FROM job_sources WHERE source = 'lever:acme'") == (1,)  # the boards were read


@pytest.mark.parametrize("reply", [[NOTICE], [listing()], {"jobs": []}])
def test_a_reply_that_looks_wrong_needs_investigation_and_closes_nothing(conn, data_dir, reply):
    run(conn, data_dir, feed(listing()))
    lines, _ = run(conn, data_dir, {remoteok.API: reply})
    assert any(line.startswith("Needs investigation (1 source;") for line in lines)
    assert any(line.startswith("  - Remote OK: ") for line in lines)
    assert state(conn) == [("ML Engineer", "open", None)]


def test_a_job_gone_from_the_feed_closes_and_a_job_back_on_it_reopens(conn, data_dir):
    run(conn, data_dir, feed(listing(), listing(OTHER_LINK, "ML Intern")))
    run(conn, data_dir, feed(listing(OTHER_LINK, "ML Intern")))
    assert state(conn)[0] == ("ML Engineer", "no longer available", jobs.NOT_IN_FEED)
    run(conn, data_dir, feed(listing(), listing(OTHER_LINK, "ML Intern")))
    assert state(conn)[0] == ("ML Engineer", "open", None)
    assert conn.execute("SELECT listed, unlisted_on, unlisted_reason FROM job_sources").fetchall() == [
        (1, None, None), (1, None, None)]  # fmt: skip


def put_on_tracker(conn, how):
    job_id = one(conn, "SELECT id FROM jobs WHERE title = 'ML Engineer'")[0]
    with conn:
        if how == "tracker":
            tracker.move(conn, job_id, "ready", "applied", date(2026, 11, 4))
        elif how == "submission":
            conn.execute("INSERT INTO submissions (job_id, submitted_on, saved_on, how, address, fields, files) "
                         "VALUES (?, '2026-11-04', 'now', 'by hand', 'x', '[]', '[]')", (job_id,))  # fmt: skip
        else:
            conn.execute("INSERT INTO preparations (job_id, kind, version, sources, writer, status, draft, created_on) "
                         "VALUES (?, 'resume', 1, '[]', '{}', 'done', '{}', 'now')", (job_id,))  # fmt: skip
    return job_id


def test_a_job_on_the_tracker_gone_from_the_feed_closes_and_keeps_its_card(conn, data_dir):
    run(conn, data_dir, feed(listing(), listing(OTHER_LINK, "ML Intern")))
    job_id = put_on_tracker(conn, "tracker")
    run(conn, data_dir, feed(listing(OTHER_LINK, "ML Intern")))
    assert state(conn)[0] == ("ML Engineer", "no longer available", jobs.NOT_IN_FEED)
    assert tracker.stage(conn, job_id) == "applied"


def test_switching_a_feed_off_closes_its_jobs_and_keeps_them_findable(conn, data_dir):
    run(conn, data_dir, feed(listing()))
    lines, web = run(conn, data_dir, feed(listing()), OFF)
    assert remoteok.API not in asked(web)
    assert "Remote OK: switched off, 1 no longer available: 1 feed turned off" in lines
    assert state(conn) == [("ML Engineer", "no longer available", jobs.FEED_OFF)]
    assert entry(conn)[0] == 0 and entry(conn)[2] == jobs.FEED_OFF
    assert one(conn, "SELECT not_interested FROM jobs") == (0,)  # closed, never hidden


@pytest.mark.parametrize("how", ["tracker", "submission", "finished resume"])
def test_switching_a_feed_off_never_touches_a_job_on_the_tracker(conn, data_dir, how):
    run(conn, data_dir, feed(listing(), listing(OTHER_LINK, "ML Intern")))
    job_id = put_on_tracker(conn, how)
    stage, before = tracker.stage(conn, job_id), entry(conn)
    run(conn, data_dir, {}, OFF)
    assert state(conn) == [("ML Engineer", "open", None), ("ML Intern", "no longer available", jobs.FEED_OFF)]
    assert one(conn, "SELECT listed, unlisted_reason FROM job_sources WHERE job_id = ?", job_id) == (1, None)
    assert tracker.stage(conn, job_id) == stage and before == (1, None, None)


def test_switching_a_feed_off_leaves_a_job_a_board_lists_open(conn, data_dir):
    pages = feed(listing()) | {LEVER: [BOARD_JOB]}
    run(conn, data_dir, pages, extra_boards=["lever:acme"])
    run(conn, data_dir, pages, OFF, extra_boards=["lever:acme"])
    assert state(conn) == [("ML Engineer", "open", None)]
    assert entry(conn)[2] == jobs.FEED_OFF


def test_switching_it_on_again_reopens_what_is_listed_and_names_what_vanished(conn, data_dir):
    run(conn, data_dir, feed(listing(), listing(OTHER_LINK, "ML Intern")))
    run(conn, data_dir, {}, OFF)
    run(conn, data_dir, feed(listing()))
    assert state(conn) == [("ML Engineer", "open", None), ("ML Intern", "no longer available", jobs.NOT_IN_FEED)]


def test_switching_off_twice_changes_nothing_and_says_nothing(conn, data_dir):
    run(conn, data_dir, feed(listing()))
    run(conn, data_dir, {}, OFF)
    before = entry(conn), state(conn)
    lines, _ = run(conn, data_dir, {}, OFF)
    assert not [line for line in lines if "Remote OK" in line]
    assert (entry(conn), state(conn)) == before


def test_no_feeds_means_no_request_to_any_feed(conn, data_dir):
    _, web = run(conn, data_dir, feed(listing()), OFF)
    assert not {request.url.host for request in web.requests} & fetch.FEED_HOSTS


def test_a_feed_never_becomes_a_row_in_sources(conn, data_dir):
    run(conn, data_dir, feed(listing()))
    run(conn, data_dir, {remoteok.API: 500})
    run(conn, data_dir, {}, OFF)
    assert one(conn, "SELECT count(*) FROM sources WHERE name LIKE 'feed:%'") == (0,)


def test_closing_the_dashboard_stops_before_the_feeds_request(conn, data_dir):
    class Closing(Exception):
        pass

    web = FakeWeb(feed(listing()) | {LEVER: []})

    def stop_after_the_boards():
        if LEVER in asked(web):
            raise Closing

    config = settings(data_dir, ["lever:acme"]).model_copy(
        update={"sources": SourceSettings(feeds=["remoteok"], extra_boards=["lever:acme"])}
    )
    with closing(fetch.Fetcher(httpx.MockTransport(web))) as fetcher, pytest.raises(Closing):
        collect.collect(conn, config, fetch.Guarded(fetcher, stop_after_the_boards), [].append)
    assert remoteok.API not in asked(web)
    assert one(conn, "SELECT count(*) FROM job_sources WHERE source = ?", FEED) == (0,)


def test_a_fault_while_saving_a_feed_leaves_nothing_half_saved(conn, data_dir, monkeypatch):
    def broken(*_):
        raise sqlcipher3.OperationalError("disk I/O error")

    monkeypatch.setattr(jobs, "_refresh_status", broken)
    with pytest.raises(sqlcipher3.OperationalError):
        run(conn, data_dir, feed(listing()))
    assert one(conn, "SELECT count(*) FROM jobs") == (0,)
    assert one(conn, "SELECT count(*) FROM job_sources") == (0,)


def test_a_reply_with_unusable_entries_closes_nothing_and_says_so(conn, data_dir):
    three = [listing(f"https://remoteok.com/remote-jobs/{n}", f"ML Role {n}") for n in "abc"]
    run(conn, data_dir, feed(*three))
    lines, _ = run(conn, data_dir, feed(three[0], {"company": "No title"}, {"position": "No company"}))
    assert ("Remote OK: 1 matching (0 new, 0 updated, 0 no longer available), 2 unusable entries skipped, "
            "so nothing was closed") in lines  # fmt: skip
    assert conn.execute("SELECT DISTINCT status FROM jobs").fetchall() == [("open",)]


def test_one_unusable_entry_is_said_in_good_english(conn, data_dir):
    lines, _ = run(conn, data_dir, feed(listing(), {"company": "No title"}))
    assert "1 unusable entry skipped, so nothing was closed" in [line for line in lines if "Remote OK" in line][0]


SURROGATE = "\ud800"


def hostile(data):
    """A reply holding a lone surrogate: valid JSON, but no text that can be saved."""
    return httpx.Response(200, text=json.dumps(data))


@pytest.mark.parametrize(
    "changes",
    [
        {"company": f"Acme{SURROGATE}"},
        {"position": f"ML Engineer{SURROGATE}"},
        {"location": f"Austin{SURROGATE}, TX"},
        {"url": f"{LINK}{SURROGATE}"},
    ],
)
def test_a_lone_surrogate_on_a_feed_stops_nothing(conn, data_dir, changes):
    lines, _ = run(conn, data_dir, {remoteok.API: hostile([NOTICE, listing() | changes])})
    assert "No sources failed." in lines
    assert not [value for row in conn.execute("SELECT * FROM jobs") for value in row if SURROGATE in str(value)]


def test_a_lone_surrogate_on_a_board_stops_nothing(conn, data_dir):
    board = [BOARD_JOB | {"text": f"ML Engineer{SURROGATE}", "categories": {"location": f"Austin{SURROGATE}, TX"}}]
    lines, _ = run(conn, data_dir, {LEVER: hostile(board)}, OFF, extra_boards=["lever:acme"])
    assert "No sources failed." in lines
    assert one(conn, "SELECT title FROM jobs") == ("ML Engineer",)


def test_deep_unclosed_tags_on_a_feed_skip_only_their_entry(conn, data_dir):
    lines, _ = run(conn, data_dir, feed(listing(), listing(OTHER_LINK) | {"description": "<script>" * 5000}))
    assert "No sources failed." in lines
    assert one(conn, "SELECT count(*) FROM jobs") == (1,)


def test_deep_unclosed_tags_on_a_board_skip_only_their_entry(conn, data_dir):
    deep = BOARD_JOB | {"id": "l-2", "hostedUrl": "https://jobs.lever.co/acme/l-2", "description": "<script>" * 5000}
    lines, _ = run(conn, data_dir, {LEVER: [BOARD_JOB, deep]}, OFF, extra_boards=["lever:acme"])
    assert "No sources failed." in lines
    assert one(conn, "SELECT count(*) FROM jobs") == (1,)


def test_show_finds_a_job_by_its_feed_listing_after_a_board_took_over_the_link(conn, data_dir):
    run(conn, data_dir, feed(listing()))
    run(conn, data_dir, feed(listing()) | {LEVER: [BOARD_JOB]}, extra_boards=["lever:acme"])
    assert one(conn, "SELECT apply_url FROM jobs") == ("https://jobs.lever.co/acme/l-1",)
    assert "  - Remote OK: listed" in collect.show(conn, LINK).splitlines()


def test_show_and_messages_name_the_feed(conn, data_dir):
    run(conn, data_dir, feed(listing()))
    assert "  - Remote OK: listed" in collect.show(conn, LINK).splitlines()
    assert collect.source_label(FEED) == "Remote OK"
    assert collect.source_label("feed:gone") == "gone"  # a feed the app no longer has
