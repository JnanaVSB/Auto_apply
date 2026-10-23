"""The collect and add commands, end to end, with a fake web and a throwaway encrypted database."""

import json
import threading
import time
from contextlib import closing

import httpx
import pytest
import sqlcipher3
from helpers import ACME, ACME_JOB, LIST, FakeWeb, add_job, board_job, entry, one, settings

from app import collect, fetch, jobs
from app.config import Config

pytestmark = pytest.mark.usefixtures("no_waiting")  # downloads never really wait

GLOBEX = "https://api.lever.co/v0/postings/globex?mode=json"


def run(conn, data_dir, pages, extra_boards=(), **web_options):
    """One collection against a fake web; returns the printed lines and the web's record of requests."""
    web, lines = FakeWeb(pages, **web_options), []
    with closing(fetch.Fetcher(httpx.MockTransport(web))) as fetcher:
        collect.collect(conn, settings(data_dir, extra_boards), fetcher, lines.append)
    return lines, web


def requested(web):
    return [str(request.url) for request in web.requests if request.method == "GET"]


def list_request(web):
    return next(request for request in web.requests if str(request.url) == LIST)


PAGES = {
    LIST: ([entry(), entry(id="s-2", title="Accountant", category="Finance", url="https://acme.example/2")], '"v1"'),
    ACME: {"jobs": [board_job()]},
}


def test_full_run_saves_matching_jobs_and_prints_a_summary(conn, data_dir):
    lines, _ = run(conn, data_dir, PAGES | {GLOBEX: []}, extra_boards=["lever:globex"])
    assert lines == [
        "Simplify Summer2027-Internships: 1 matching (1 new, 0 updated, 0 no longer available)",
        "Reading 2 company boards, all sites at the same time (about 1 minute)...",
        "Company boards: 2 of 2 read, 1 matching (0 new, 1 updated, 0 no longer available)",
        "Total: 1 new, 1 updated, 0 no longer available.",
        "No sources failed.",
    ]
    assert one(conn, "SELECT count(*), max(description), max(status) FROM jobs") == (1, "Train models.", "open")
    assert one(conn, "SELECT etag FROM sources WHERE name = 'simplify:Summer2027-Internships'") == ('"v1"',)
    assert one(conn, "SELECT outcome, new_jobs, updated_jobs, failures FROM collect_runs") == ("done", 1, 1, "[]")


def test_second_run_creates_no_duplicates_and_skips_an_unchanged_list(conn, data_dir):
    run(conn, data_dir, PAGES)
    lines, web = run(conn, data_dir, PAGES)
    assert lines[0] == "Simplify Summer2027-Internships: unchanged since the last run"
    assert lines[2] == "Company boards: 1 of 1 read, 1 matching (0 new, 0 updated, 0 no longer available)"
    assert list_request(web).headers["If-None-Match"] == '"v1"'
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
    assert "If-None-Match" not in list_request(web).headers  # downloaded afresh


def test_boards_dropped_by_simplify_are_still_read(conn, data_dir):
    run(conn, data_dir, PAGES)
    other = board_job(id=999, absolute_url="https://job-boards.greenhouse.io/acme/jobs/999", title="Accountant")
    _, web = run(conn, data_dir, {LIST: ([entry(url="https://acme.example/jobs/1")], '"v2"'), ACME: {"jobs": [other]}})
    assert ACME in requested(web)
    assert one(conn, "SELECT status_reason FROM jobs WHERE apply_url = ?", ACME_JOB) == ("removed from its board",)


def test_cancelling_keeps_finished_sources_and_is_recorded(conn, data_dir):
    with pytest.raises(KeyboardInterrupt):
        run(conn, data_dir, PAGES | {ACME: KeyboardInterrupt()})
    assert one(conn, "SELECT count(*) FROM jobs") == (1,)  # the Simplify list was saved before stopping
    assert one(conn, "SELECT outcome FROM collect_runs") == ("cancelled",)


def test_a_second_copy_is_refused(tmp_path):
    with closing(collect.lock(tmp_path)):
        with pytest.raises(collect.CollectError, match="already running"):
            collect.lock(tmp_path)
    collect.lock(tmp_path).close()  # free again once the first run has finished


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
        ("https://simplify.jobs./p/123", "must not open"),
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
    for missing in (tmp_path / "nothing-here", tmp_path):  # no data folder; a folder without app.db
        code, err = main([], settings(missing))
        assert code == 1
        assert "not found" in err
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


def test_a_board_not_found_three_runs_in_a_row_is_retired_and_named_once(conn, data_dir):
    pages = PAGES | {ACME: 404}
    summaries = [run(conn, data_dir, pages)[0] for _ in range(3)]
    stopped = [line for lines in summaries for line in lines if line.startswith("Stopped reading")]
    assert stopped == [
        "Stopped reading 1 board not found 3 runs in a row; their jobs are marked 'board shut down' and Simplify "
        "now decides for any it also lists (read again if Simplify links a newer job there, or if you add them to "
        "extra_boards):"
    ]
    assert summaries[2][-1] == "  - Greenhouse board 'acme'"
    assert board_state(conn)[0] == 3
    lines, web = run(conn, data_dir, pages)
    assert ACME not in requested(web)
    assert lines[-1] == "No sources failed."
    assert one(conn, "SELECT status FROM jobs") == ("open",)  # Simplify still lists it, so it stays open


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


def test_a_job_you_added_keeps_its_label_when_its_board_is_read_again(conn, data_dir):
    london = {"jobs": [board_job(title="Accountant", location={"name": "London, UK"})]}
    with closing(fetch.Fetcher(httpx.MockTransport(FakeWeb({ACME: london})))) as fetcher:
        collect.add(conn, fetcher, ACME_JOB)
    run(conn, data_dir, {LIST: 404, ACME: london})
    assert one(conn, "SELECT us_location, added_by_you, check_location FROM jobs") == ("unclear", 1, 0)


def test_a_retired_boards_jobs_are_decided_by_simplify(conn, data_dir):
    run(conn, data_dir, PAGES)  # the board lists the job, so the board decides
    for _ in range(3):
        run(conn, data_dir, {LIST: 404, ACME: 404})
    assert one(conn, "SELECT status FROM jobs") == ("open",)  # retired; Simplify still lists it
    run(conn, data_dir, {LIST: ([entry(active=False)], '"v2"')})  # now Simplify drops it too
    assert one(conn, "SELECT status, status_reason FROM jobs") == ("no longer available", "board shut down")
    assert conn.execute("SELECT source, unlisted_reason FROM job_sources ORDER BY rowid").fetchall() == [
        ("simplify:Summer2027-Internships", "inactive on Simplify"),
        ("greenhouse:acme", "board shut down"),  # same day, so the board's reason is shown
    ]


def test_a_board_you_stopped_is_never_read_and_no_longer_decides(conn, data_dir):
    run(conn, data_dir, PAGES)  # the board lists the job
    with conn:
        conn.execute("UPDATE sources SET stopped_on = '2026-10-22' WHERE name = 'greenhouse:acme'")
    newer = entry(id="s-4", url="https://job-boards.greenhouse.io/acme/jobs/6", date_posted=int(time.time()) + 86400)
    _, web = run(conn, data_dir, {LIST: ([entry(), newer], '"v2"'), ACME: {"jobs": [board_job()]}})
    assert ACME not in requested(web)  # not even when Simplify links a newer job there
    run(conn, data_dir, {LIST: ([entry(active=False), newer], '"v3"')})  # Simplify drops the job: it decides now
    assert one(conn, "SELECT status, status_reason FROM jobs WHERE title = 'ML Intern' ORDER BY id") == (
        "no longer available", "inactive on Simplify")  # fmt: skip
    _, web = run(conn, data_dir, {ACME: {"jobs": [board_job()]}}, extra_boards=["greenhouse:acme"])
    assert ACME in requested(web)  # listed in extra_boards by you: that wins
    assert one(conn, "SELECT count(*) FROM jobs")[0] >= 1  # nothing deleted


def test_a_board_empty_three_runs_in_a_row_means_the_company_stopped_hiring(conn, data_dir):
    run(conn, data_dir, PAGES)
    summaries = [run(conn, data_dir, {LIST: 404, ACME: {"jobs": []}})[0] for _ in range(3)]
    for number, lines in enumerate(summaries[:2], 1):
        assert lines[-1] == (
            "  - Greenhouse board 'acme': sent no jobs, but its last good read listed 1 job "
            f"(empty {number} of 3 runs in a row; then its jobs are marked 'company stopped hiring')"
        )
    assert summaries[2][1:3] == [
        "Company boards: 1 of 1 read, 0 matching (0 new, 0 updated, 1 no longer available: 1 company stopped hiring)",
        "Total: 0 new, 0 updated, 1 no longer available: 1 company stopped hiring.",
    ]
    assert summaries[2][-2:] == [
        "Company stopped hiring (1 board empty 3 runs in a row; their jobs are kept and marked "
        "'company stopped hiring'):",
        "  - Greenhouse board 'acme'",
    ]
    assert one(conn, "SELECT status, status_reason FROM jobs") == ("no longer available", "company stopped hiring")
    lines, _ = run(conn, data_dir, {LIST: 404, ACME: {"jobs": []}})  # still empty: nothing more to say
    assert not any("acme" in line for line in lines)


def test_only_empty_runs_in_a_row_mean_the_company_stopped_hiring(conn, data_dir):
    run(conn, data_dir, PAGES)
    empty = {LIST: 404, ACME: {"jobs": []}}
    for pages in (empty, {LIST: 404, ACME: 500}):  # a server error counts for nothing
        run(conn, data_dir, pages)
    assert "(empty 2 of 3 runs in a row;" in run(conn, data_dir, empty)[0][-1]
    run(conn, data_dir, PAGES)  # jobs again: start counting afresh
    assert "(empty 1 of 3 runs in a row;" in run(conn, data_dir, empty)[0][-1]
    assert one(conn, "SELECT status FROM jobs") == ("open",)


def test_show_prints_a_jobs_status_and_every_source(conn, data_dir):
    run(conn, data_dir, PAGES)
    run(conn, data_dir, {LIST: ([entry(id="s-9", url="https://acme.example/9")], '"v2"'), ACME: {"jobs": []}})
    today = one(conn, "SELECT unlisted_on FROM job_sources WHERE source LIKE 'simplify:%'")[0]
    assert collect.show(conn, ACME_JOB + "?gh_src=x") == "\n".join([
        "Acme, ML Intern",
        "Status: open",
        "Sources:",
        f"  - Simplify Summer2027-Internships: removed from Simplify (since {today})",
        "  - Greenhouse board 'acme': listed",  # an empty board needs investigation first, so nothing changed
    ])  # fmt: skip
    with pytest.raises(collect.CollectError, match="No saved job has that link"):
        collect.show(conn, "https://acme.example/never-saved")


def test_the_lock_is_taken_before_the_database_is_opened(main, data_dir, monkeypatch):
    monkeypatch.setattr(collect.db, "connect", lambda folder: pytest.fail("opened before the lock"))
    with closing(collect.lock(data_dir)):
        assert main([], settings(data_dir)) == (1, "Error: A collection is already running. Wait for it to finish.\n")


def test_no_internet_stops_at_once(conn, data_dir):
    with pytest.raises(collect.CollectError, match="No internet connection"):
        run(conn, data_dir, PAGES, down=fetch.ALLOWED_HOSTS)
    assert one(conn, "SELECT count(*) FROM collect_runs") == (0,)


def test_an_unreachable_site_is_skipped_with_one_line_and_closes_nothing(conn, data_dir):
    run(conn, data_dir, PAGES)
    lines, web = run(conn, data_dir, PAGES, down=["boards-api.greenhouse.io"])
    assert ACME not in requested(web)
    assert lines[-2:] == [
        "1 source failed:",
        "  - boards-api.greenhouse.io: could not be reached; 1 source skipped this run, closing nothing "
        "(the site may be down, or your network may be blocking it)",
    ]
    assert one(conn, "SELECT status FROM jobs") == ("open",)


def test_a_site_that_stops_answering_mid_run_is_skipped_after_five_boards(conn, data_dir):
    boards = [f"greenhouse:board{number}" for number in range(9)]
    lines, web = run(conn, data_dir, {}, extra_boards=boards, silent=["boards-api.greenhouse.io"])
    board_requests = [url for url in requested(web) if "boards-api.greenhouse.io" in url]
    tried = len(set(board_requests))
    assert 5 <= tried <= 5 + collect.IN_FLIGHT - 1  # five in a row, plus any already being downloaded
    assert len(board_requests) == tried * 4  # each tried four times, then the site is skipped
    hint = "(the site may be down, or your network may be blocking it)"
    assert f"connection failed (ConnectError); gave up after 3 retries {hint}" in lines[-2]
    assert lines[-1] == (
        f"  - boards-api.greenhouse.io: could not be reached; {9 - tried} sources skipped this run, closing nothing "
        f"{hint}"
    )


def test_each_board_remembers_who_found_it_first(conn, data_dir):
    run(conn, data_dir, PAGES | {GLOBEX: []}, extra_boards=["lever:globex", "ashby:gone"])  # ashby:gone: not found
    run(conn, data_dir, PAGES, extra_boards=["greenhouse:acme"])  # listing it yourself later changes nothing
    assert dict(conn.execute("SELECT name, found_by FROM sources").fetchall()) == {
        "simplify:Summer2027-Internships": "you",  # your settings
        "greenhouse:acme": "simplify",
        "lever:globex": "you",
        "ashby:gone": "you",
    }


def test_boards_the_phone_book_found_are_read_after_the_others_on_their_site(conn, data_dir, monkeypatch):
    monkeypatch.setattr(collect, "IN_FLIGHT", 1)  # one download per site, so the order is exact
    with conn:
        conn.executemany(
            "INSERT INTO sources (name, found_by) VALUES (?, ?)",
            [("greenhouse:aaa", "commoncrawl"), ("greenhouse:bbb", "commoncrawl"), ("greenhouse:zzz", "simplify")],
        )
        add_job(conn, "ML Intern", sources=["greenhouse:bbb"])  # a job saved from it: no longer "discovered"
    _, web = run(conn, data_dir, {LIST: 404})
    names = [url.split("/")[-2] for url in requested(web) if "boards-api.greenhouse.io" in url]
    assert names == ["bbb", "zzz", "aaa"]


def run_with(conn, data_dir, pages, answer, extra_boards):
    """One collection whose web layer is `answer(request, web)`, for tests that hold requests open."""
    web = FakeWeb(pages)
    with closing(fetch.Fetcher(httpx.MockTransport(lambda request: answer(request, web)))) as fetcher:
        collect.collect(conn, settings(data_dir, extra_boards), fetcher, [].append)


def test_all_sites_are_read_at_the_same_time(conn, data_dir):
    lever_asked = threading.Event()

    def answer(request, web):
        if request.method == "GET" and request.url.host == "api.lever.co":
            lever_asked.set()
        if request.method == "GET" and request.url.host == "boards-api.greenhouse.io":
            assert lever_asked.wait(5)  # read one site after another, Greenhouse would wait here for Lever
        return web(request)

    run_with(conn, data_dir, PAGES | {GLOBEX: []}, answer, ["lever:globex"])
    assert one(conn, "SELECT count(*) FROM sources WHERE last_ok IS NOT NULL") == (3,)  # the list and both boards


def test_up_to_in_flight_downloads_are_open_at_once_on_one_site(conn, data_dir):
    boards = [f"greenhouse:board{number}" for number in range(collect.IN_FLIGHT)]
    together = threading.Barrier(collect.IN_FLIGHT, timeout=5)  # passes only when all are open at the same time

    def answer(request, web):
        if request.method == "GET" and request.url.host == "boards-api.greenhouse.io":
            together.wait()
        return web(request)

    run_with(conn, data_dir, {LIST: 404}, answer, boards)
    assert not together.broken


@pytest.mark.parametrize(
    ("page", "reason"),
    [
        ({"jobs": [{"id": 123, "absolute_url": ACME_JOB}]}, "none of its 1 entries could be read"),  # no title
        ({"jobs": []}, "sent no jobs, but its last good read listed 1 job"),
        ({"postings": []}, "unexpected format"),
    ],
)
def test_a_board_whose_data_looks_wrong_needs_investigation_and_changes_nothing(conn, data_dir, page, reason):
    run(conn, data_dir, PAGES)
    lines, _ = run(conn, data_dir, {LIST: 404, ACME: page})
    heading = lines.index(
        "Needs investigation (1 source; the data looks different from usual, so nothing was changed for them):"
    )
    assert lines[heading + 1].startswith(f"  - Greenhouse board 'acme': {reason}")
    assert one(conn, "SELECT status, description FROM jobs") == ("open", "Train models.")


def test_a_board_that_never_had_jobs_may_be_empty(conn, data_dir):
    lines, _ = run(conn, data_dir, {GLOBEX: []}, extra_boards=["lever:globex"])
    assert not any(line.startswith("Needs investigation") for line in lines)


def test_a_reshaped_simplify_list_needs_investigation(conn, data_dir):
    lines, _ = run(conn, data_dir, {LIST: ([{"renamed": "fields"}], '"v1"')})
    assert any(line.startswith("  - Simplify Summer2027-Internships: none of its 1 entries") for line in lines)


def test_an_unreachable_simplify_list_is_skipped(conn, data_dir):
    lines, web = run(conn, data_dir, PAGES, down=["raw.githubusercontent.com"])
    assert LIST not in requested(web)
    assert lines[-1].startswith("  - raw.githubusercontent.com: could not be reached; 1 source skipped this run")


def test_only_failures_in_a_row_skip_a_site(conn, data_dir):
    boards = [f"greenhouse:board{number}" for number in range(9)]
    urls = [f"https://boards-api.greenhouse.io/v1/boards/board{number}/jobs?content=true" for number in range(9)]
    pages = {url: httpx.ConnectError("no route to host") for url in urls} | {urls[4]: {"jobs": []}}  # board4 answers
    lines, web = run(conn, data_dir, pages, extra_boards=boards)
    assert set(urls) <= set(requested(web))  # 4 failures, an answer, 4 failures: never 5 in a row
    assert not any("could not be reached" in line for line in lines)
