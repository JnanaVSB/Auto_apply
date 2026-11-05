"""The export beside the dashboard: when it is due, one at a time, and plain words whatever goes wrong. Made-up jobs,
a scratch folder and a stand-in for the PDF build."""

import logging
import threading
import time
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest
import sqlcipher3
from helpers import one
from test_export import built, by_hand, root  # noqa: F401 (fixtures)

from app import collect, config, db, export, tracker
from app.documents import pdf
from app.export import folder
from app.web import exporter as exporter_module
from app.web.exporter import BUSY, Exporter

SECRET = "Sam-Lee-Park-private-answer"  # noqa: S105 (a made-up answer, not a password)
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


@pytest.fixture
def settings(tmp_path, data_dir, monkeypatch):
    """A made-up settings file; returns a function that rewrites it with this folder and schedule."""

    def write(folder="", every_days=0):
        path = tmp_path / "config.yaml"
        path.write_text(f'data_dir: {data_dir}\nexport:\n  folder: "{folder}"\n  every_days: {every_days}\n', "utf-8")
        monkeypatch.setattr(config, "CONFIG_PATH", path)
        return path

    write()
    return write


@pytest.fixture
def exporter(data_dir):
    return Exporter(data_dir)


@pytest.fixture
def clock(monkeypatch):
    """The exporter's clock, set by hand, starting at NOW."""

    class Clock(datetime):
        at = NOW

        @classmethod
        def now(cls, tz=None):
            return cls.at

    monkeypatch.setattr(exporter_module, "datetime", Clock)
    return Clock


@pytest.fixture
def runs(monkeypatch):
    """A stand-in for export.run: keeps each call's arguments and thread, and gives `runs.result`."""

    class Runs(list):
        result = export.Result(selected=1, folders=1, files=2)
        gate = None

    calls = Runs()

    def run(conn, data_dir, text, start, end, unexported=False, step=None):
        calls.append({"text": text, "start": start, "end": end, "unexported": unexported,
                      "thread": threading.current_thread(), "step": step})  # fmt: skip
        if calls.gate:
            calls.gate.wait(10)
        return calls.result

    monkeypatch.setattr(exporter_module.export, "run", run)
    return calls


def finish(exporter):
    thread = exporter._thread
    if thread:
        thread.join(10)
    assert not exporter.running


def tried(conn, moment):
    with conn:
        conn.execute("INSERT INTO export_runs (id, tried_on) VALUES (1, ?) ON CONFLICT (id) DO UPDATE SET "
                     "tried_on = excluded.tried_on", (moment.isoformat(timespec="seconds"),))  # fmt: skip


def good(conn, moment):
    with conn:
        conn.execute("INSERT INTO export_runs (id, good_on) VALUES (1, ?) ON CONFLICT (id) DO UPDATE SET "
                     "good_on = excluded.good_on", (moment,))  # fmt: skip


def saved(conn):
    return conn.execute("SELECT tried_on, good_on, last_result FROM export_runs").fetchall()


def test_off_by_default(conn, settings, exporter, runs):
    exporter.tick(conn)
    finish(exporter)
    assert runs == []


def test_on_with_no_folder_waits(conn, settings, exporter, runs):
    settings("", 7)
    exporter.tick(conn)
    finish(exporter)
    assert runs == [] and saved(conn) == []


def test_a_folder_with_the_schedule_off_is_nothing(conn, settings, exporter, runs, tmp_path):
    settings(str(tmp_path), 0)
    exporter.tick(conn)
    finish(exporter)
    assert runs == []


def test_settings_that_dont_load_are_nothing(conn, settings, exporter, runs):
    settings().write_text("every_days: [", encoding="utf-8")
    exporter.tick(conn)
    finish(exporter)
    assert runs == []


def test_no_run_yet_is_due_at_once_and_never_on_the_callers_thread(conn, settings, exporter, runs, tmp_path):
    settings(str(tmp_path), 7)
    exporter.tick(conn)
    finish(exporter)
    assert len(runs) == 1 and runs[0]["thread"] is not threading.current_thread()
    assert runs[0]["text"] == str(tmp_path) and runs[0]["unexported"] is True and runs[0]["end"] is None


@pytest.mark.parametrize(("ago", "due"), [(timedelta(days=7), True), (timedelta(days=7, seconds=-1), False),
                                          (timedelta(days=30), True), (timedelta(0), False)])  # fmt: skip
def test_due_at_exactly_the_days_set_and_not_a_second_before(conn, settings, exporter, runs, clock, tmp_path, ago, due):
    settings(str(tmp_path), 7)
    tried(conn, NOW - ago)
    exporter.tick(conn)
    finish(exporter)
    assert len(runs) == due


def test_two_checks_start_one_run(conn, settings, exporter, runs, tmp_path):
    settings(str(tmp_path), 7)
    runs.gate = threading.Event()
    exporter.tick(conn)
    exporter.tick(conn)
    runs.gate.set()
    finish(exporter)
    exporter.tick(conn)  # and the next minute, nothing either
    finish(exporter)
    assert len(runs) == 1


def test_a_stopped_run_waits_the_days_too_and_keeps_the_last_good_run(conn, settings, exporter, runs, tmp_path):
    settings(str(tmp_path), 7)
    good(conn, "2026-10-01T10:00:00+00:00")
    runs.result = export.Result(selected=3, files=1, stopped="No space left on device: stopped after 1 of 3")
    exporter.tick(conn)
    finish(exporter)
    tried_on, good_on, last_result = saved(conn)[0]
    assert good_on == "2026-10-01T10:00:00+00:00" and tried_on
    assert "1 file added" in last_result and "No space left on device: stopped after 1 of 3" in last_result
    assert last_result == exporter.notice
    fresh = Exporter(exporter.data_dir)  # the try is saved: a dashboard opened again waits too
    fresh.tick(conn)
    finish(fresh)
    assert len(runs) == 1


def test_a_finished_run_saves_when_and_what(conn, settings, exporter, runs, tmp_path):
    settings(str(tmp_path))
    assert exporter.start(None, None) is None
    finish(exporter)
    tried_on, good_on, last_result = saved(conn)[0]
    assert tried_on and good_on and last_result == "1 new folder, 2 files added" == exporter.notice


def test_a_press_counts_as_the_last_try(conn, settings, exporter, runs, tmp_path):
    settings(str(tmp_path), 7)
    exporter.start(date(2026, 10, 1), date(2026, 10, 31))
    finish(exporter)
    exporter.tick(conn)
    finish(exporter)
    assert len(runs) == 1 and (runs[0]["start"], runs[0]["end"]) == (date(2026, 10, 1), date(2026, 10, 31))
    assert runs[0]["unexported"] is False


def test_a_folder_that_fails_the_check_says_why_and_waits(conn, settings, exporter, built, by_hand_job, tmp_path):  # noqa: F811
    (tmp_path / "exports" / ".git").mkdir(parents=True)
    settings(str(tmp_path / "exports"), 7)
    exporter.tick(conn)
    finish(exporter)
    assert "git project" in exporter.notice and "git project" in saved(conn)[0][2] and saved(conn)[0][1] is None
    exporter.tick(conn)
    finish(exporter)
    assert one(conn, "SELECT count(*) FROM exported")[0] == 0


def test_a_run_stopped_before_it_chose_any_saves_the_reason_alone(conn, settings, exporter, tmp_path):
    settings(str(tmp_path / "unplugged"))
    exporter.start(None, None)
    finish(exporter)
    assert exporter.notice == saved(conn)[0][2] == folder.GONE


def test_tectonic_missing_saves_the_reason_alone(conn, settings, exporter, built, root, monkeypatch):  # noqa: F811
    settings(str(root))
    monkeypatch.setattr(pdf, "tectonic", lambda: None)
    exporter.start(None, None)
    finish(exporter)
    assert exporter.notice == saved(conn)[0][2] == f"Tectonic isn't installed: install it with: {pdf.INSTALL}"


@pytest.fixture
def by_hand_job(conn, data_dir):
    return by_hand(conn, data_dir)


def schedule_start(conn, exporter, settings, runs, tmp_path, every_days):
    settings(str(tmp_path), every_days)
    exporter.tick(conn)
    finish(exporter)
    return runs[-1]["start"]


def test_with_no_good_run_it_starts_the_days_back(conn, settings, exporter, runs, tmp_path):
    assert schedule_start(conn, exporter, settings, runs, tmp_path, 7) == date.today() - timedelta(days=7)


def test_a_last_good_run_older_than_the_days_goes_back_to_it(conn, settings, exporter, runs, tmp_path):
    good(conn, "2026-01-05T12:00:00+00:00")
    assert schedule_start(conn, exporter, settings, runs, tmp_path, 7) == date(2026, 1, 5)


def test_a_last_good_run_newer_than_the_days_still_starts_the_days_back(conn, settings, exporter, runs, tmp_path):
    recent = (datetime.now(UTC) - timedelta(days=1)).isoformat(timespec="seconds")
    good(conn, recent)
    assert schedule_start(conn, exporter, settings, runs, tmp_path, 7) == date.today() - timedelta(days=7)


@pytest.mark.parametrize(("place", "day"), [("Pacific/Auckland", date(2026, 10, 20)),
                                            ("America/Los_Angeles", date(2026, 10, 19))])  # fmt: skip
def test_the_last_good_run_is_read_as_a_local_date(conn, settings, exporter, runs, tmp_path, monkeypatch, place, day):
    monkeypatch.setenv("TZ", place)
    time.tzset()
    try:
        good(conn, "2026-10-19T23:30:00+00:00")
        assert schedule_start(conn, exporter, settings, runs, tmp_path, 7) == day
    finally:
        monkeypatch.undo()
        time.tzset()


def test_a_three_week_gap_and_a_backdated_application_lose_nothing(conn, data_dir, settings, exporter, built, root):  # noqa: F811
    settings(str(root), 1)
    gap = (date.today() - timedelta(days=21)).isoformat()
    good(conn, f"{gap}T12:00:00+00:00")
    tried(conn, datetime.now(UTC) - timedelta(days=21))
    in_gap = by_hand(conn, data_dir, "In the gap", on=date.today() - timedelta(days=10))
    backdated = by_hand(conn, data_dir, "Backdated", on=date(2026, 1, 5))
    exporter.tick(conn)
    finish(exporter)
    assert {row[0] for row in conn.execute("SELECT job_id FROM exported")} == {in_gap, backdated}


def test_a_second_press_while_one_runs_is_told_so(conn, settings, exporter, runs, tmp_path):
    settings(str(tmp_path))
    runs.gate = threading.Event()
    assert exporter.start(None, None) is None and exporter.running
    assert exporter.start(None, None) == BUSY == "An export is running"
    runs.gate.set()
    finish(exporter)
    assert len(runs) == 1


def test_a_press_while_the_schedule_runs_is_told_so(conn, settings, exporter, runs, tmp_path):
    settings(str(tmp_path), 7)
    runs.gate = threading.Event()
    exporter.tick(conn)
    assert exporter.start(None, None) == BUSY
    runs.gate.set()
    finish(exporter)


def test_a_press_right_after_a_run_ended_works(conn, settings, exporter, runs, tmp_path):
    settings(str(tmp_path))
    for _ in range(3):
        assert exporter.start(None, None) is None
        finish(exporter)
    assert len(runs) == 3


def test_a_second_dashboard_says_busy_and_saves_nothing(conn, data_dir, settings, exporter, runs, tmp_path):
    settings(str(tmp_path))
    with closing(collect.lock(data_dir, exporter_module.LOCK, BUSY)):
        exporter.start(None, None)
        finish(exporter)
    assert exporter.notice == BUSY and runs == [] and saved(conn) == []


def test_the_lock_is_free_again_after_a_run(data_dir, settings, exporter, runs, tmp_path):
    settings(str(tmp_path))
    exporter.start(None, None)
    finish(exporter)
    collect.lock(data_dir, exporter_module.LOCK, BUSY).close()


def test_the_thread_uses_a_connection_of_its_own_and_closes_it(conn, settings, exporter, monkeypatch, tmp_path):
    settings(str(tmp_path))
    opened, real = [], db.connect

    def connect(data_dir):
        found = real(data_dir)
        opened.append(found)
        return found

    monkeypatch.setattr(exporter_module.db, "connect", connect)
    monkeypatch.setattr(exporter_module.export, "run", lambda given, *_, **__: export.Result(selected=0))
    exporter.start(None, None)
    finish(exporter)
    assert len(opened) == 1 and opened[0] is not conn
    with pytest.raises(sqlcipher3.ProgrammingError):
        opened[0].execute("SELECT 1")


def test_progress_is_there_for_the_page_while_it_runs(data_dir, settings, exporter, monkeypatch, tmp_path):
    settings(str(tmp_path))
    seen, reached, gate = [], threading.Event(), threading.Event()

    def run(conn, data_dir, text, start, end, unexported=False, step=None):
        step(0, 40)
        step(12, 40)
        seen.append((exporter.done, exporter.total))
        reached.set()
        gate.wait(10)
        return export.Result(selected=40)

    monkeypatch.setattr(exporter_module.export, "run", run)
    exporter.start(None, None)
    assert reached.wait(10) and seen == [(12, 40)] and exporter.running
    gate.set()
    finish(exporter)


def test_a_new_run_starts_from_nothing(data_dir, settings, exporter, runs, tmp_path):
    settings(str(tmp_path))
    exporter.done, exporter.total, exporter.problems = 9, 9, ["old line"]
    runs.gate = threading.Event()
    exporter.start(None, None)
    assert (exporter.done, exporter.total, exporter.problems) == (0, 0, [])
    runs.gate.set()
    finish(exporter)


def test_a_crash_leaves_no_old_problem_lines_on_the_page(settings, exporter, monkeypatch, tmp_path):
    settings(str(tmp_path))
    exporter.problems = ["a line from the run before"]
    monkeypatch.setattr(exporter_module.export, "run", unexpected)
    exporter.start(None, None)
    finish(exporter)
    assert exporter.problems == []


def unexpected(*_, **__):
    raise RuntimeError(f"{SECRET} in /home/someone/exports")


def test_an_unexpected_error_never_leaves_the_runner_stuck(conn, settings, exporter, monkeypatch, tmp_path, caplog):
    settings(str(tmp_path))
    monkeypatch.setattr(exporter_module.export, "run", unexpected)
    with caplog.at_level(logging.DEBUG):
        exporter.start(None, None)
        finish(exporter)
    assert exporter.notice == exporter_module.UNEXPECTED and SECRET not in exporter.notice
    assert SECRET not in caplog.text and "/home/someone" not in caplog.text and "RuntimeError" in caplog.text
    monkeypatch.setattr(exporter_module.export, "run", lambda *_, **__: export.Result(selected=0))
    assert exporter.start(None, None) is None
    finish(exporter)
    assert exporter.notice == "No applications between those dates"


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_failure_of_the_thread_itself_never_leaves_the_runner_stuck(settings, exporter, monkeypatch, tmp_path):
    settings(str(tmp_path))
    monkeypatch.setattr(exporter, "_words", unexpected)
    exporter.start(None, None)
    finish(exporter)
    monkeypatch.setattr(exporter, "_words", lambda _: "Done.")
    assert exporter.start(None, None) is None
    finish(exporter)
    assert exporter.notice == "Done."


def test_an_unexpected_error_still_waits_the_days(conn, settings, exporter, monkeypatch, tmp_path):
    settings(str(tmp_path), 7)
    monkeypatch.setattr(exporter_module.export, "run", unexpected)
    exporter.tick(conn)
    finish(exporter)
    assert saved(conn)[0][0] and saved(conn)[0][1] is None
    calls = []
    monkeypatch.setattr(exporter_module.export, "run", lambda *args, **__: calls.append(args))
    Exporter(exporter.data_dir).tick(conn)
    assert calls == []


def test_a_busy_database_is_said_plainly(settings, exporter, monkeypatch, tmp_path, caplog):
    settings(str(tmp_path))

    def busy(*_, **__):
        raise sqlcipher3.OperationalError("database is locked")

    monkeypatch.setattr(exporter_module.export, "run", busy)
    exporter.start(None, None)
    finish(exporter)
    assert "the database could not be read or written" in exporter.notice
    assert exporter.start(None, None) is None
    finish(exporter)


def test_an_error_opening_the_database_is_said_plainly(settings, exporter, monkeypatch, tmp_path):
    settings(str(tmp_path))
    monkeypatch.setattr(exporter_module.db, "connect", lambda *_: (_ for _ in ()).throw(db.DatabaseError("No key.")))
    exporter.start(None, None)
    finish(exporter)
    assert exporter.notice == "Error: No key."


def test_settings_that_stop_loading_inside_the_thread_are_said_plainly(conn, settings, exporter, runs):
    settings().write_text("export:\n  every_days: 999\n", encoding="utf-8")
    exporter.start(None, None)
    finish(exporter)
    assert exporter.notice.startswith("The settings can't be read") and runs == []
    assert exporter.start(None, None) is None
    finish(exporter)


def test_problem_lines_stay_in_memory(conn, settings, exporter, runs, tmp_path, caplog):
    settings(str(tmp_path))
    runs.result = export.Result(selected=1, files=1, problems=[f"{SECRET}, ML Engineer: job_description.pdf: no text"])
    with caplog.at_level(logging.DEBUG):
        exporter.start(None, None)
        finish(exporter)
    assert exporter.problems == runs.result.problems
    assert SECRET not in str(saved(conn)) and SECRET not in caplog.text and SECRET not in exporter.notice


def test_a_real_run_end_to_end(conn, data_dir, settings, exporter, built, root, caplog):  # noqa: F811
    job = by_hand(conn, data_dir, company=SECRET)
    tracker.move(conn, job, "applied", "offer", date(2026, 10, 20))
    settings(str(root))
    with caplog.at_level(logging.DEBUG):
        exporter.start(None, None)
        finish(exporter)
    assert exporter.notice == "1 new folder, 5 files added" and exporter.problems == []
    assert (exporter.done, exporter.total) == (1, 1) and saved(conn)[0][2] == exporter.notice
    assert SECRET not in caplog.text and str(root) not in caplog.text
