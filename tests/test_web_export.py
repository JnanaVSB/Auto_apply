"""Settings → Export: the warning, the folder, Export now, the schedule and what the last run said, on a made-up
settings file with a stand-in for the run."""

import asyncio
import logging
import stat
from datetime import date
from pathlib import Path

import pytest
import yaml
from helpers import csrf, log_in
from test_export import built, by_hand, root  # noqa: F401 (fixtures)
from test_web_settings import form as settings_form

from app import config
from app.config import PROJECT_DIR, ExportSettings, load_config
from app.export import folder
from app.web import export_settings, main
from app.web.exporter import BUSY, Exporter

PAGE = "/settings/export"
WARNING = (
    "These files are not encrypted. Anyone who can open the folder can read them: your resume, your application "
    "history and every answer you gave, including your expected salary, your work authorization, and any answers about "
    "your gender, race, disability or veteran status. Choose a folder only you can reach. A folder that syncs to a "
    "cloud service (Dropbox, OneDrive, iCloud) uploads them."
)
ROUTES = ("/folder", "/run", "/schedule")


@pytest.fixture
def settings(tmp_path, data_dir, monkeypatch):
    """A made-up settings file in a folder of its own; returns a function that rewrites it."""
    path = tmp_path / "settings" / "config.yaml"
    path.parent.mkdir()
    monkeypatch.setattr(config, "CONFIG_PATH", path)

    def write(**export):
        text = {"data_dir": str(data_dir), **({"export": export} if export else {})}
        path.write_text(yaml.safe_dump(text, allow_unicode=True), encoding="utf-8")
        return path

    write()
    return write


@pytest.fixture
def client(client, settings):
    log_in(client)
    return client


@pytest.fixture
def started(client, monkeypatch):
    """Presses of Export, kept and not run."""
    found = []
    monkeypatch.setattr(client.app.state.exporter, "start", lambda start, end: found.append((start, end)))
    return found


def post(client, route, **form):
    return client.post(PAGE + route, data=form | {"csrf_token": csrf(client)}, follow_redirects=False)


def followed(client, route, **form):
    return client.post(PAGE + route, data=form | {"csrf_token": csrf(client)})


def saved():
    return load_config().export


@pytest.fixture
def chosen(tmp_path, settings):
    """A folder saved in the settings."""
    found = tmp_path / "exports"
    found.mkdir()
    settings(folder=str(found))
    return found


def test_the_tick_is_a_tick_box_beside_its_words_in_both_forms(client):
    assert client.get(PAGE).text.count('<label class="tick"><input type="checkbox" name="tick">') == 2


def test_how_it_is_going_comes_right_under_the_warning_and_notices(client):
    text = followed(client, "/folder", folder="relative").text
    assert (
        text.index(WARNING) < text.index('role="alert"') < text.index("Export status") < text.index("<h2>Folder</h2>")
    )
    assert text.index('id="export-status"') < text.index("<h2>Folder</h2>")


def test_the_page_can_be_reached_from_settings(client):
    assert f'href="{PAGE}"' in client.get("/settings").text


def test_every_route_needs_a_login(client):
    client.cookies.clear()
    assert client.get(PAGE, follow_redirects=False).status_code == 303
    assert client.get(f"{PAGE}/status", follow_redirects=False).status_code == 303
    for route in ROUTES:
        assert client.post(PAGE + route, follow_redirects=False).status_code == 303


@pytest.mark.parametrize("route", ROUTES)
def test_every_press_needs_its_anti_forgery_token(client, started, route):
    before = saved()
    assert client.post(PAGE + route, data={"tick": "on", "folder": "/x", "every_days": "3"}).status_code == 403
    assert started == [] and saved() == before


def test_the_warning_stands_above_everything_word_for_word(client):
    text = client.get(PAGE).text
    assert WARNING in text and text.index(WARNING) < text.index("<h2>")


@pytest.mark.parametrize(
    "press",
    [("/run", {}), ("/run", {"tick": "on"}), ("/folder", {"folder": "relative"}), ("/schedule", {"every_days": "7"}),
     ("/schedule", {"every_days": "abc"})],
)  # fmt: skip
def test_the_warning_is_there_after_every_refusal_too(client, press):
    response = followed(client, press[0], **press[1])
    assert response.status_code == 400 and WARNING in response.text


@pytest.mark.parametrize("word", ["salary", "work authorization", "gender", "race", "disability", "veteran", "cloud"])
def test_the_warning_names_what_is_in_the_files(client, word):
    assert word in client.get(PAGE).text


def test_a_new_folder_is_made_for_you_only_and_saved_as_it_really_is(client, tmp_path):
    response = post(client, "/folder", folder=f"  {tmp_path}/a/../exports  ")
    assert response.status_code == 303 and saved().folder == str(tmp_path / "exports")
    assert stat.S_IMODE((tmp_path / "exports").stat().st_mode) == 0o700


def test_a_first_folder_says_saved_and_a_change_says_earlier_exports_stay(client, tmp_path, chosen):
    text = followed(client, "/folder", folder=str(tmp_path / "other")).text
    assert "Earlier exports stay where they are." in text and saved().folder == str(tmp_path / "other")
    assert chosen.is_dir()  # moved or deleted nothing


def test_saving_the_folder_keeps_the_schedule_and_saving_the_schedule_keeps_the_folder(client, settings, tmp_path):
    settings(every_days=7)
    post(client, "/folder", folder=str(tmp_path / "exports"))
    assert saved() == ExportSettings(folder=str(tmp_path / "exports"), every_days=7)
    post(client, "/schedule", every_days="0")
    assert saved() == ExportSettings(folder=str(tmp_path / "exports"), every_days=0)


def test_the_main_settings_form_keeps_both(client, settings, tmp_path):
    settings(folder=str(tmp_path), every_days=7)
    client.post("/settings", data=settings_form() | {"csrf_token": csrf(client)})
    assert saved() == ExportSettings(folder=str(tmp_path), every_days=7)


@pytest.mark.parametrize(
    ("typed", "words"),
    [("", folder.CHOOSE), ("   ", folder.CHOOSE), ("relative/path", "full path"), ("~", "home folder"),
     ("/", "home folder"), (str(PROJECT_DIR), "project"), (str(PROJECT_DIR / "app"), "project")],
)  # fmt: skip
def test_each_refused_folder_shows_its_sentence_and_saves_nothing(client, settings, typed, words):
    before = config.CONFIG_PATH.read_bytes()
    response = followed(client, "/folder", folder=typed)
    assert response.status_code == 400 and words in response.text and config.CONFIG_PATH.read_bytes() == before


def test_the_data_folder_a_git_project_a_file_and_a_deep_path_are_refused_here_too(client, data_dir, tmp_path):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    for typed, words in ((data_dir / "x", "data folder"), (tmp_path / "repo", "git project"),
                         (tmp_path / "file.txt", "file"), (tmp_path / "new" / "deeper", "last part")):  # fmt: skip
        response = followed(client, "/folder", folder=str(typed))
        assert response.status_code == 400 and words in response.text and saved().folder == ""
    assert not (tmp_path / "new").exists()


def test_a_refused_path_is_kept_in_the_box_escaped(client):
    typed = '<script>alert("x")</script>&"'
    text = followed(client, "/folder", folder=typed).text
    assert "<script>alert" not in text and "&lt;script&gt;alert(" in text


def test_a_saved_path_with_odd_characters_is_shown_escaped_and_never_in_an_address(client, tmp_path, caplog):
    typed = str(tmp_path / 'a<b>&"c')
    with caplog.at_level(logging.DEBUG):
        response = post(client, "/folder", folder=typed)
    assert response.status_code == 303 and "a<b>" not in response.headers["location"]
    assert "a%3Cb" not in response.headers["location"] and typed not in caplog.text
    assert saved().folder == typed
    text = client.get(PAGE).text
    assert "a<b>" not in text and "a&lt;b&gt;&amp;" in text


def test_a_settings_file_that_cant_be_saved_is_said_plainly(client, settings, tmp_path):
    config.CONFIG_PATH.parent.chmod(0o500)
    try:
        response = followed(client, "/folder", folder=str(tmp_path / "exports"))
        schedule = followed(client, "/schedule", every_days="0")
    finally:
        config.CONFIG_PATH.parent.chmod(0o700)
    assert response.status_code == 400 and "could not be saved" in response.text and schedule.status_code == 400


def test_a_settings_file_that_doesnt_load_still_shows_the_page(client, started, settings):
    settings().write_text("export:\n  every_days: 999\n", encoding="utf-8")
    text = client.get(PAGE).text
    assert "export.every_days" in text and WARNING in text
    assert followed(client, "/run", tick="on").status_code == 400 and started == []


def test_export_needs_the_tick_checked_on_the_server(client, started, chosen):
    for tick in ({}, {"tick": ""}, {"tick": "off"}, {"tick": "no"}):
        response = followed(client, "/run", **tick)
        assert response.status_code == 400 and "encrypted" in response.text
    assert started == []


def test_export_needs_a_folder(client, started):
    response = followed(client, "/run", tick="on")
    assert response.status_code == 400 and folder.CHOOSE in response.text and started == []


@pytest.mark.parametrize(
    ("first", "last", "dates"),
    [("", "", (None, None)), ("2026-10-01", "", (date(2026, 10, 1), None)),
     ("", "2026-10-31", (None, date(2026, 10, 31))),
     ("2026-10-01", "2026-10-31", (date(2026, 10, 1), date(2026, 10, 31))),
     ("2026-10-01", "2026-10-01", (date(2026, 10, 1), date(2026, 10, 1))),
     (" 2026-10-01 ", "", (date(2026, 10, 1), None))],
)  # fmt: skip
def test_export_takes_the_dates_as_typed(client, started, chosen, first, last, dates):
    response = post(client, "/run", tick="on", start=first, end=last)
    assert (response.status_code, response.headers["location"]) == (303, PAGE) and started == [dates]


@pytest.mark.parametrize(
    ("first", "last"),
    [("2026-13-01", ""), ("2026-02-30", ""), ("<script>x</script>", ""),
     ("yesterday", ""), ("2026-1-5", ""), ("٢٠٢٦-١٠-٠١", "")],
)  # fmt: skip
def test_a_date_that_isnt_one_is_refused_in_a_plain_sentence(client, started, chosen, first, last):
    response = followed(client, "/run", tick="on", start=first, end=last)
    assert response.status_code == 400 and "year-month-day" in response.text and started == []
    assert "<script>x" not in response.text


def test_to_before_from_is_refused(client, started, chosen):
    response = followed(client, "/run", tick="on", start="2026-10-31", end="2026-10-01")
    assert response.status_code == 400 and "From date comes first" in response.text and started == []


def test_a_second_press_says_it_is_running(client, chosen, monkeypatch):
    monkeypatch.setattr(client.app.state.exporter, "start", lambda *_: BUSY)
    response = followed(client, "/run", tick="on")
    assert response.status_code == 400 and BUSY in response.text


@pytest.mark.parametrize("days", ["-1", "366", "1.5", "abc", "", "²", "1e2", "7 days"])
def test_a_schedule_outside_0_to_365_is_refused_in_a_plain_sentence(client, settings, chosen, days):
    settings(folder=str(chosen), every_days=3)
    before = config.CONFIG_PATH.read_bytes()
    for tick in ({"tick": "on"}, {}):
        response = followed(client, "/schedule", every_days=days, **tick)
        assert response.status_code == 400 and "Give the days as a whole number from 0 to 365." in response.text
        assert "export.every_days" not in response.text and "Invalid settings" not in response.text
    assert config.CONFIG_PATH.read_bytes() == before


def test_a_schedule_refused_keeps_what_was_typed_escaped(client, chosen):
    text = followed(client, "/schedule", every_days='"><script>', tick="on").text
    assert "<script>" not in text and "&lt;script&gt;" in text


def test_the_settings_check_stays_behind_the_page(client, monkeypatch, chosen):
    monkeypatch.setattr(export_settings, "MOST_DAYS", 1000)
    response = followed(client, "/schedule", every_days="999", tick="on")
    assert response.status_code == 400 and "export.every_days" in response.text


def test_a_schedule_above_0_needs_the_tick_and_0_doesnt(client, chosen):
    for tick in ({}, {"tick": "off"}):
        response = followed(client, "/schedule", every_days="7", **tick)
        assert response.status_code == 400 and "encrypted" in response.text and saved().every_days == 0
    assert post(client, "/schedule", every_days="7", tick="on").status_code == 303 and saved().every_days == 7
    assert post(client, "/schedule", every_days="0").status_code == 303 and saved().every_days == 0
    assert post(client, "/schedule", every_days=" 365 ", tick="on").status_code == 303 and saved().every_days == 365


def test_with_the_schedule_off_the_page_says_what_the_first_run_will_do(client, chosen):
    text = client.get(PAGE).text
    assert "The first run exports every application not exported yet." in text and "next minute check" in text
    assert "Runs only while the dashboard is open" in text and "Next run" not in text


def test_with_the_schedule_on_and_no_run_yet_it_says_the_next_minute_check(client, settings, chosen):
    settings(folder=str(chosen), every_days=7)
    text = client.get(PAGE).text
    assert "Last run" not in text and "next minute check" in text


def test_with_the_schedule_on_it_says_the_last_run_and_the_next(client, conn, settings, chosen):
    settings(folder=str(chosen), every_days=7)
    with conn:
        conn.execute("INSERT INTO export_runs (id, tried_on) VALUES (1, '2026-10-17T12:00:00+00:00')")
    text = client.get(PAGE).text
    assert "Last run 2026-10-17" in text and "Next run 2026-10-24" in text


def test_a_schedule_with_no_folder_waits_and_says_why(client, settings):
    settings(every_days=7)
    assert "waits until you choose a folder" in client.get(PAGE).text


def test_the_page_shows_the_saved_result_after_a_restart_but_no_problem_lines(client, conn, data_dir):
    with conn:
        conn.execute("INSERT INTO export_runs (id, last_result) VALUES (1, '2 new folders, 3 files added')")
    exporter = client.app.state.exporter
    exporter.problems = ["Acme, ML Engineer: resume.pdf: file was missing"]
    assert "Acme, ML Engineer" in client.get(PAGE).text
    client.app.state.exporter = Exporter(data_dir)  # a new dashboard
    text = client.get(PAGE).text
    assert "2 new folders, 3 files added" in text and "Acme, ML Engineer" not in text
    client.app.state.exporter = exporter


def test_the_last_runs_own_words_come_before_the_saved_ones(client, conn):
    with conn:
        conn.execute("INSERT INTO export_runs (id, last_result) VALUES (1, '2 new folders')")
    client.app.state.exporter.notice = BUSY
    assert BUSY in client.get(PAGE).text


def test_problem_lines_and_results_are_shown_escaped(client):
    exporter = client.app.state.exporter
    exporter.problems, exporter.notice = ["<b>Acme</b>, ML: resume.pdf: file was missing"], "<i>words</i>"
    for address in (PAGE, f"{PAGE}/status"):
        text = client.get(address).text
        assert "<b>Acme" not in text and "&lt;b&gt;Acme" in text and "<i>words" not in text
    exporter.problems, exporter.notice = [], None


def test_while_it_runs_the_status_line_asks_every_3_seconds_and_stops_at_the_end(client):
    exporter = client.app.state.exporter
    exporter._thread, exporter.done, exporter.total = object(), 12, 40
    for address in (PAGE, f"{PAGE}/status"):
        text = client.get(address).text
        assert "Exporting 12 of 40 applications…" in text and f'hx-get="{PAGE}/status"' in text
        assert 'hx-trigger="every 3s"' in text
    exporter._thread, exporter.notice = None, "1 new folder"
    text = client.get(f"{PAGE}/status").text
    assert "hx-get" not in text and "1 new folder" in text and "Exporting" not in text
    exporter.notice = None


def test_before_it_has_counted_it_only_says_exporting(client):
    exporter = client.app.state.exporter
    exporter._thread, exporter.done, exporter.total = object(), 0, 0
    text = client.get(f"{PAGE}/status").text
    assert "Exporting…" in text and "of 0" not in text
    exporter._thread = None


def test_the_status_part_is_a_part_not_a_page(client):
    assert "<html" not in client.get(f"{PAGE}/status").text


def test_no_notice_is_left_saying_it_is_exporting(client):
    assert "how far it got" not in client.get(f"{PAGE}?done=started").text


def test_a_press_really_runs_and_the_page_says_what_happened(client, conn, data_dir, settings, built, root):  # noqa: F811
    by_hand(conn, data_dir)
    settings(folder=str(root))
    assert post(client, "/run", tick="on").status_code == 303
    thread = client.app.state.exporter._thread
    if thread:
        thread.join(10)
    assert "1 new folder, 5 files added" in client.get(PAGE).text
    assert [path.name for path in root.iterdir()] == ["2026-10-17"]


def test_the_minute_check_includes_the_export(client, monkeypatch):
    called = []

    def stop(conn):
        called.append("export")
        raise asyncio.CancelledError

    state = client.app.state
    for runner in (state.collector, state.scorer, state.preparer, state.mailer):
        monkeypatch.setattr(runner, "tick", lambda conn: None)
    monkeypatch.setattr(state.exporter, "tick", stop)
    monkeypatch.setattr(main, "SCHEDULE_CHECK_SECONDS", 0)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main._schedule(client.app))
    assert called == ["export"]


def test_main_gets_only_its_three_touches():
    source = Path(main.__file__).read_text(encoding="utf-8")
    assert source.count("export_settings") == 2 and source.count("exporter") == 1


def test_a_press_with_nothing_to_export_says_so_in_plain_words(client, chosen, built):  # noqa: F811
    post(client, "/run", tick="on")
    thread = client.app.state.exporter._thread
    if thread:
        thread.join(10)
    assert "No applications to export yet" in client.get(PAGE).text
