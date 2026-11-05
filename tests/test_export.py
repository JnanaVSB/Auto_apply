"""The export's run, with a stand-in for the PDF build: made-up people and jobs, and a scratch folder, never data/."""

import logging
import os
import stat
from contextlib import closing
from datetime import date
from pathlib import Path

import pytest
from helpers import add_job

from app import db, export, tracker
from app.council import runs
from app.documents import pdf
from app.export import folder, papers
from app.submit import record

ON = date(2026, 10, 17)
DONE = """INSERT INTO preparations (job_id, kind, version, sources, writer, status, draft, created_on)
    VALUES (?, ?, ?, '[]', '{}', 'done', '{}', 'then')"""
ALL_FILES = ["council_notes.pdf", "filled_answers.pdf", "job_description.pdf", "resume.pdf", "status_history.txt"]
SECRET = "Sam-Lee-Park-private-answer"  # noqa: S105 (a made-up answer, not a password)


@pytest.fixture
def root(tmp_path):
    found = tmp_path / "exports"
    found.mkdir()
    return found


@pytest.fixture
def built(monkeypatch):
    """A stand-in for Tectonic: writes a small file and keeps each build's LaTeX. `built.fail` is a function of the
    build's number, from 1, that says whether it fails; `built.missing` the characters each build reports."""

    class Builds(list):
        fail = staticmethod(lambda number: False)
        missing = ()
        refused = ()  # words: a build whose text holds one fails
        during = staticmethod(lambda target: None)  # called in the middle of each build

    builds = Builds()

    def build(data_dir, target, source):
        builds.append((target, source))
        builds.during(target)
        if builds.fail(len(builds)) or any(word in " ".join(source.split()) for word in builds.refused):
            raise pdf.PdfError("The PDF could not be built: a made-up reason.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"%PDF " + source.encode())
        target.with_suffix(".json").write_text("{}", encoding="utf-8")
        return pdf.Built(1, tuple(builds.missing))

    monkeypatch.setattr(pdf, "build_kept", build)
    monkeypatch.setattr(pdf, "tectonic", lambda: Path("/usr/bin/tectonic"))
    return builds


def finished(conn, data_dir, job, version=1, kind="resume", body=None):
    """A finished version of a job's resume or letter, with its PDF."""
    with conn:
        conn.execute(DONE, (job, kind, version))
    target = pdf.tailored(data_dir, job, version, kind)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body if body is not None else f"%PDF {kind} v{version}".encode())
    return target


def job_row(conn, title="ML Engineer", company="Acme", description="We train robots."):
    with conn:
        return add_job(conn, title, company=company, description=description)


def by_hand(conn, data_dir, title="ML Engineer", on=ON, company="Acme", letter=False):
    """A made-up job you applied to and recorded, on a resume v1 (and a letter)."""
    job = job_row(conn, title, company)
    finished(conn, data_dir, job)
    if letter:
        finished(conn, data_dir, job, kind=runs.LETTER)
    address = conn.execute("SELECT apply_url FROM jobs WHERE id = ?", (job,)).fetchone()[0]
    assert record.submit(conn, data_dir, {"id": job, "apply_url": address}, on, None) is None
    return job


def no_record(conn, title="ML Engineer", to="applied", on=ON, company="Acme"):
    job = job_row(conn, title, company)
    assert tracker.move(conn, job, "ready", to, on)
    return job


def run(conn, data_dir, root, start=None, end=None, step=lambda done, total: None, **options):
    return export.run(conn, data_dir, str(root), start, end, step, **options)


def where(conn, root, job):
    return root / conn.execute("SELECT folder FROM exported WHERE job_id = ?", (job,)).fetchone()[0]


def listing(root):
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))


def state(root):
    """Every file's bytes and time, so a repeat that touches anything shows."""
    return {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in root.rglob("*") if path.is_file()}


def rows(conn, table):
    return conn.execute(f'SELECT * FROM "{table}"').fetchall()  # noqa: S608 (test helper)


def everything(conn):
    names = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").fetchall()
    return {name: rows(conn, name) for (name,) in names if name != "exported"}


def test_a_record_is_listed_once_on_the_date_you_chose_not_the_cards(conn, data_dir):
    job = by_hand(conn, data_dir, on=date(2026, 10, 17))
    with conn:
        conn.execute("UPDATE tracker SET applied_on = '2026-10-20' WHERE job_id = ?", (job,))
    assert export.applications(conn, None, None) == [(job, "2026-10-17")]


def test_a_record_stays_listed_with_its_card_moved_back_or_gone(conn, data_dir):
    back, gone = by_hand(conn, data_dir), by_hand(conn, data_dir, "Other")
    tracker.move(conn, back, "applied", "ready", ON)
    with conn:
        conn.execute("DELETE FROM tracker WHERE job_id = ?", (gone,))
    assert {found for found, _ in export.applications(conn, None, None)} == {back, gone}


def test_a_ready_card_with_no_record_isnt_listed_and_one_moved_back_neither(conn, data_dir):
    finished(conn, data_dir, job_row(conn, "Only ready"))
    back = no_record(conn, "Moved back")
    tracker.move(conn, back, "applied", "ready", ON)
    assert export.applications(conn, None, None) == []


def test_a_card_rejected_straight_from_ready_is_listed_on_its_date(conn):
    job = no_record(conn, to="rejected", on=date(2026, 10, 20))
    assert export.applications(conn, None, None) == [(job, "2026-10-20")]


def test_both_ends_of_the_dates_are_included(conn, data_dir):
    days = [date(2026, 10, day) for day in (16, 17, 18, 19, 20)]
    jobs = [by_hand(conn, data_dir, f"Job {day}", on=day) for day in days]
    assert [found for found, _ in export.applications(conn, days[1], days[3])] == jobs[1:4]
    assert [found for found, _ in export.applications(conn, days[3], None)] == jobs[3:]
    assert [found for found, _ in export.applications(conn, None, days[1])] == jobs[:2]
    assert export.applications(conn, days[3], days[1]) == []  # To before From: the page refuses it, nothing here


def test_the_order_is_the_date_then_the_job(conn, data_dir):
    later = by_hand(conn, data_dir, "Later", on=date(2026, 10, 20))
    first, second = by_hand(conn, data_dir, "One"), by_hand(conn, data_dir, "Two")
    assert [found for found, _ in export.applications(conn, None, None)] == [first, second, later]


def test_a_date_of_tomorrow_is_listed_with_no_end_date(conn, data_dir):
    with conn:
        job = by_hand(conn, data_dir)
        conn.execute("UPDATE submissions SET submitted_on = '2099-01-01' WHERE job_id = ?", (job,))
    assert export.applications(conn, date(2098, 1, 1), None) == [(job, "2099-01-01")]


def test_unfinished_also_takes_what_isnt_exported_whatever_its_date(conn, data_dir, root, built):
    old = by_hand(conn, data_dir, "Old", on=date(2026, 1, 5))
    done = by_hand(conn, data_dir, "Done", on=date(2026, 1, 6))
    recent = by_hand(conn, data_dir, "Recent", on=date(2026, 10, 20))
    run(conn, data_dir, root, date(2026, 1, 6), date(2026, 1, 6))
    window = date(2026, 10, 1), None
    assert export.applications(conn, *window) == [(recent, "2026-10-20")]
    assert export.applications(conn, *window, unfinished=True) == [(old, "2026-01-05"), (recent, "2026-10-20")]
    assert done not in dict(export.applications(conn, *window, unfinished=True))


def test_a_first_export_makes_every_file_for_a_record(conn, data_dir, root, built):
    job = by_hand(conn, data_dir, letter=True)
    result = run(conn, data_dir, root)
    assert sorted(path.name for path in where(conn, root, job).iterdir()) == sorted([*ALL_FILES, "cover_letter.pdf"])
    assert where(conn, root, job).relative_to(root).as_posix() == "2026-10-17/Acme_ML-Engineer"
    assert (result.folders, result.again, result.files, result.stopped, result.problems) == (1, 0, 6, None, [])


def test_a_card_with_no_record_gets_what_exists_and_no_filled_answers(conn, data_dir, root, built):
    job = no_record(conn)
    finished(conn, data_dir, job)
    result = run(conn, data_dir, root)
    folder_ = where(conn, root, job)
    assert sorted(path.name for path in folder_.iterdir()) == [
        "council_notes.pdf", "job_description.pdf", "resume.pdf", "status_history.txt",
    ]  # fmt: skip
    assert (folder_ / "status_history.txt").read_text(encoding="utf-8").startswith(papers.NO_RECORD)
    assert result.problems == []


def test_a_card_with_no_record_and_no_version_gets_its_text_and_history(conn, data_dir, root, built):
    job = no_record(conn)
    run(conn, data_dir, root)
    assert sorted(path.name for path in where(conn, root, job).iterdir()) == [
        "job_description.pdf",
        "status_history.txt",
    ]


def test_only_the_version_the_record_names_is_copied(conn, data_dir, root, built):
    job = by_hand(conn, data_dir)
    finished(conn, data_dir, job, version=2, body=b"%PDF newer")
    run(conn, data_dir, root)
    assert (where(conn, root, job) / "resume.pdf").read_bytes() == b"%PDF resume v1"


def test_a_record_with_no_files_never_falls_back_to_the_newest(conn, data_dir, root, built):
    job = no_record(conn)
    address = conn.execute("SELECT apply_url FROM jobs WHERE id = ?", (job,)).fetchone()[0]
    record.submit(conn, data_dir, {"id": job, "apply_url": address}, ON, None)  # past Ready, no version yet
    finished(conn, data_dir, job, version=1)
    run(conn, data_dir, root)
    assert not (where(conn, root, job) / "resume.pdf").exists()
    assert "No files were recorded" in filled(built)


def filled(built):
    """The filled answers' LaTeX, with its source lines joined."""
    texts = (" ".join(source.split()).replace(r"\textquotesingle{}", "'") for _, source in built)
    return next(text for text in texts if "What the app prepared" in text)


def test_a_pdf_gone_is_left_out_and_said_on_the_page_and_in_the_paper(conn, data_dir, root, built):
    job = by_hand(conn, data_dir)
    pdf.tailored(data_dir, job, 1).unlink()
    result = run(conn, data_dir, root)
    assert not (where(conn, root, job) / "resume.pdf").exists()
    assert papers.GONE in filled(built) and [line for line in result.problems if papers.GONE in line]
    assert record.MISSING not in filled(built) and not [line for line in result.problems if record.MISSING in line]


def test_a_pdf_rebuilt_since_is_copied_and_said_to_differ(conn, data_dir, root, built):
    job = by_hand(conn, data_dir)
    pdf.tailored(data_dir, job, 1).write_bytes(b"%PDF rebuilt")
    result = run(conn, data_dir, root)
    assert (where(conn, root, job) / "resume.pdf").read_bytes() == b"%PDF rebuilt"
    assert papers.NOT_RECORDED in filled(built) and [line for line in result.problems if papers.NOT_RECORDED in line]


def test_a_0_byte_pdf_on_a_record_differs_from_its_hash(conn, data_dir, root, built):
    job = by_hand(conn, data_dir)
    pdf.tailored(data_dir, job, 1).write_bytes(b"")
    run(conn, data_dir, root)
    assert papers.NOT_RECORDED in filled(built)


def test_a_0_byte_pdf_on_a_card_with_no_record_is_skipped_and_said(conn, data_dir, root, built):
    job = no_record(conn)
    finished(conn, data_dir, job, body=b"")
    result = run(conn, data_dir, root)
    assert not (where(conn, root, job) / "resume.pdf").exists() and [
        line for line in result.problems if "empty" in line
    ]


def test_a_file_missing_when_you_applied_but_back_now_is_copied_and_says_both(conn, data_dir, root, built):
    job = job_row(conn)
    target = finished(conn, data_dir, job)
    target.unlink()  # gone when you applied
    address = conn.execute("SELECT apply_url FROM jobs WHERE id = ?", (job,)).fetchone()[0]
    record.submit(conn, data_dir, {"id": job, "apply_url": address}, ON, None)
    target.write_bytes(b"%PDF back")
    run(conn, data_dir, root)
    assert (where(conn, root, job) / "resume.pdf").read_bytes() == b"%PDF back"
    assert record.NO_COPY in filled(built) and papers.NOT_RECORDED in filled(built)


def test_a_resume_already_there_and_not_the_version_named_is_left_and_said(conn, data_dir, root, built):
    """A card exported with no record, then recorded: the copy stays, and the paper tells it isn't the one named."""
    job = no_record(conn)
    target = finished(conn, data_dir, job, body=b"%PDF older copy")
    run(conn, data_dir, root)
    target.write_bytes(b"%PDF the one you applied with")
    address = conn.execute("SELECT apply_url FROM jobs WHERE id = ?", (job,)).fetchone()[0]
    record.submit(conn, data_dir, {"id": job, "apply_url": address}, ON, None)
    built.clear()
    result = run(conn, data_dir, root)
    assert (where(conn, root, job) / "resume.pdf").read_bytes() == b"%PDF older copy"
    assert papers.NOT_RECORDED in filled(built) and result.beside == 1


@pytest.mark.parametrize("kind", ["folder", "link"])
def test_a_resume_that_is_a_folder_or_a_link_is_left_alone(conn, data_dir, root, built, tmp_path, kind):
    job = by_hand(conn, data_dir)
    run(conn, data_dir, root)
    place = where(conn, root, job)
    (place / "resume.pdf").unlink()
    (place / "filled_answers.pdf").unlink()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"yours")
    (place / "resume.pdf").mkdir() if kind == "folder" else (place / "resume.pdf").symlink_to(outside)
    built.clear()
    result = run(conn, data_dir, root)
    assert result.stopped is None and outside.read_bytes() == b"yours"
    assert papers.NOT_RECORDED in filled(built)  # it isn't the file recorded, whatever it is


def test_an_unknown_slot_is_a_line_and_the_rest_carries_on(conn, data_dir, root, built):
    job = by_hand(conn, data_dir)
    with conn:
        conn.execute("""UPDATE submissions SET files = '[{"slot": "portfolio", "label": "X", "version": 1, "sha256": "",
            "note": ""}, ' || substr(files, 2) WHERE job_id = ?""", (job,))  # fmt: skip
    result = run(conn, data_dir, root)
    assert (where(conn, root, job) / "resume.pdf").is_file() and len(result.problems) == 1


def test_a_run_changes_no_table_but_exported(conn, data_dir, root, built):
    by_hand(conn, data_dir)
    no_record(conn, "Other")
    before = everything(conn)
    run(conn, data_dir, root)
    assert everything(conn) == before and len(rows(conn, "exported")) == 2


def test_the_same_run_twice_changes_nothing_and_says_so(conn, data_dir, root, built):
    by_hand(conn, data_dir, letter=True)
    no_record(conn, "Other")
    run(conn, data_dir, root)
    before, exported, builds = state(root), rows(conn, "exported"), len(built)
    result = run(conn, data_dir, root)
    assert state(root) == before and rows(conn, "exported") == exported and len(built) == builds
    assert result.summary() == "Nothing new" and result.problems == []


def test_overlapping_dates_make_no_duplicates(conn, data_dir, root, built):
    jobs = [by_hand(conn, data_dir, f"Job {day}", on=date(2026, 10, day)) for day in (16, 17, 18)]
    run(conn, data_dir, root, date(2026, 10, 16), date(2026, 10, 17))
    run(conn, data_dir, root, date(2026, 10, 17), date(2026, 10, 18))
    assert len(rows(conn, "exported")) == 3 and len([name for name in listing(root) if name.count("/") == 1]) == 3
    assert all(where(conn, root, job).is_dir() for job in jobs)


def test_a_deleted_file_alone_comes_back(conn, data_dir, root, built):
    job = by_hand(conn, data_dir)
    run(conn, data_dir, root)
    before = state(root)
    (where(conn, root, job) / "job_description.pdf").unlink()
    result = run(conn, data_dir, root)
    assert state(root).keys() == before.keys() and result.files == 1 and result.summary() == "1 file added"


def test_a_deleted_folder_comes_back_with_a_run_over_its_date_and_only_then(conn, data_dir, root, built):
    job = by_hand(conn, data_dir)
    run(conn, data_dir, root)
    place = where(conn, root, job)
    for path in place.iterdir():
        path.unlink()
    place.rmdir()
    elsewhere = run(conn, data_dir, root, date(2026, 11, 1), date(2026, 11, 2))
    assert not place.exists() and elsewhere.selected == 0
    result = run(conn, data_dir, root, ON, ON)
    assert place.is_dir() and (result.folders, result.again) == (0, 1) and "1 folder made again" in result.summary()


def test_a_new_title_and_company_keep_the_folder(conn, data_dir, root, built):
    job = by_hand(conn, data_dir)
    run(conn, data_dir, root)
    place = where(conn, root, job)
    with conn:
        conn.execute("UPDATE jobs SET title = 'Other', company = 'Other Inc' WHERE id = ?", (job,))
    (place / "job_description.pdf").unlink()
    run(conn, data_dir, root)
    assert where(conn, root, job) == place and (place / "job_description.pdf").is_file()


def test_two_jobs_with_one_name_on_one_day_each_get_their_own(conn, data_dir, root, built):
    one, two = by_hand(conn, data_dir), by_hand(conn, data_dir)
    run(conn, data_dir, root)
    assert where(conn, root, one).name == "Acme_ML-Engineer" and where(conn, root, two).name == "Acme_ML-Engineer_2"


def test_a_deleted_folders_name_stays_its_own(conn, data_dir, root, built):
    first = by_hand(conn, data_dir)
    run(conn, data_dir, root)
    place = where(conn, root, first)
    for path in place.iterdir():
        path.unlink()
    place.rmdir()
    second = by_hand(conn, data_dir)
    with conn:  # different papers, so a mix-up would show
        conn.execute("UPDATE jobs SET description = 'Second job.' WHERE id = ?", (second,))
    pdf.tailored(data_dir, second, 1).write_bytes(b"%PDF second")
    run(conn, data_dir, root, date(2026, 11, 1), None, unfinished=True)
    assert where(conn, root, second).name == "Acme_ML-Engineer_2" and not place.exists()
    run(conn, data_dir, root)
    assert (place / "resume.pdf").read_bytes() == b"%PDF resume v1"
    assert (where(conn, root, second) / "resume.pdf").read_bytes() == b"%PDF second"


def test_a_new_chosen_folder_gets_a_full_copy_and_the_old_one_is_left(conn, data_dir, root, built, tmp_path):
    job = by_hand(conn, data_dir)
    run(conn, data_dir, root)
    before = state(root)
    new = tmp_path / "new"
    new.mkdir()
    result = run(conn, data_dir, new)
    assert state(root) == before and (result.folders, result.again) == (0, 1)
    assert sorted(path.name for path in where(conn, new, job).iterdir()) == ALL_FILES


@pytest.mark.parametrize("stored", ["../x", "/etc/outside", "", "a/../../x"])
def test_a_stored_folder_leading_out_is_refused_with_a_line_and_nothing_written(conn, data_dir, root, built, stored):
    job = by_hand(conn, data_dir)
    with conn:
        conn.execute("INSERT INTO exported (job_id, folder, exported_on) VALUES (?, ?, 'then')", (job, stored))
    before = listing(root.parent)
    result = run(conn, data_dir, root)
    assert listing(root.parent) == before and result.again == 0 and len(result.problems) == 1


def test_a_crash_after_the_row_heals_with_one_folder_and_one_row(conn, data_dir, root, built, monkeypatch):
    job = by_hand(conn, data_dir)
    write = folder.write
    with monkeypatch.context() as crash:
        crash.setattr(folder, "write", lambda *_: (_ for _ in ()).throw(RuntimeError("the power went")))
        with pytest.raises(RuntimeError):
            run(conn, data_dir, root)
    assert folder.write is write and len(rows(conn, "exported")) == 1  # the row came first
    result = run(conn, data_dir, root)
    assert (result.folders, result.again) == (0, 0) and len(rows(conn, "exported")) == 1
    assert sorted(path.name for path in where(conn, root, job).iterdir()) == ALL_FILES
    assert len([name for name in listing(root) if name.count("/") == 1]) == 1


def test_a_crash_between_two_papers_leaves_only_the_missing_ones_to_make(conn, data_dir, root, built, monkeypatch):
    job = by_hand(conn, data_dir)
    write = folder.write

    def crash_on_council(target, data):
        if target.name == "council_notes.pdf":
            raise RuntimeError("the power went")
        write(target, data)

    with monkeypatch.context() as crash:
        crash.setattr(folder, "write", crash_on_council)
        with pytest.raises(RuntimeError):
            run(conn, data_dir, root)
    kept = (where(conn, root, job) / "job_description.pdf").stat().st_mtime_ns
    built.clear()
    run(conn, data_dir, root)
    assert len(built) == 2 and (where(conn, root, job) / "job_description.pdf").stat().st_mtime_ns == kept


def test_a_partial_left_over_is_cleared(conn, data_dir, root, built):
    job = by_hand(conn, data_dir)
    run(conn, data_dir, root)
    (where(conn, root, job) / "job_description.pdf.partial").write_bytes(b"ha")
    (where(conn, root, job) / "status_history.txt.partial").write_bytes(b"ha")
    assert run(conn, data_dir, root).stopped is None and not list(root.rglob("*.partial"))


def test_a_partial_left_over_never_stops_the_refresh_of_an_old_application(conn, data_dir, root, built):
    job = by_hand(conn, data_dir)
    run(conn, data_dir, root)
    (where(conn, root, job) / "status_history.txt.partial").write_bytes(b"ha")
    tracker.move(conn, job, "applied", "offer", ON)
    result = run(conn, data_dir, root, date(2026, 11, 1), None)
    assert result.stopped is None and result.refreshed == 1 and not list(root.rglob("*.partial"))


def test_every_run_refreshes_old_histories_outside_its_dates(conn, data_dir, root, built):
    job = by_hand(conn, data_dir)
    run(conn, data_dir, root)
    tracker.move(conn, job, "applied", "offer", ON)
    result = run(conn, data_dir, root, date(2026, 11, 1), date(2026, 11, 2))
    assert result.refreshed == 1 and "Offer" in (where(conn, root, job) / "status_history.txt").read_text("utf-8")
    assert result.selected == 0


def test_the_refresh_makes_no_folder_and_no_missing_file(conn, data_dir, root, built):
    gone, no_history = by_hand(conn, data_dir, "One"), by_hand(conn, data_dir, "Two")
    run(conn, data_dir, root)
    for path in where(conn, root, gone).iterdir():
        path.unlink()
    where(conn, root, gone).rmdir()
    (where(conn, root, no_history) / "status_history.txt").unlink()
    before = listing(root)
    result = run(conn, data_dir, root, date(2026, 11, 1), date(2026, 11, 2))
    assert listing(root) == before and (result.refreshed, result.files) == (0, 0)


def test_a_history_you_changed_is_left_and_the_new_one_goes_beside_it_and_named(conn, data_dir, root, built):
    job = by_hand(conn, data_dir, company="Acme")
    run(conn, data_dir, root)
    history = where(conn, root, job) / "status_history.txt"
    history.write_text("my notes\n", encoding="utf-8")
    result = run(conn, data_dir, root)
    assert history.read_text(encoding="utf-8") == "my notes\n" and result.beside == 1
    assert "1 status history left alone (it no longer matches: the new one is beside it)" in result.summary()
    assert "you changed" not in result.summary() and "Acme" in result.problems[0]
    assert run(conn, data_dir, root).beside == 0  # and only once


def test_a_refresh_with_a_damaged_record_still_opens_with_its_date(conn, data_dir, root, built):
    job = by_hand(conn, data_dir)
    run(conn, data_dir, root)
    tracker.move(conn, job, "applied", "offer", ON)
    with conn:
        conn.execute("UPDATE submissions SET fields = '[not json'")
    result = run(conn, data_dir, root, date(2026, 11, 1), None)
    text = (where(conn, root, job) / "status_history.txt").read_text(encoding="utf-8")
    assert result.refreshed == 1 and text.startswith("2026-10-17  Applied\n")


def test_one_paper_that_fails_leaves_the_others(conn, data_dir, root, built):
    job = by_hand(conn, data_dir)
    built.fail = lambda number: number == 1
    result = run(conn, data_dir, root)
    assert sorted(path.name for path in where(conn, root, job).iterdir()) == [
        name for name in ALL_FILES if name != "job_description.pdf"
    ]
    (line,) = result.problems
    assert "Acme" in line and "job_description.pdf" in line and result.stopped is None
    built.fail = lambda number: False
    run(conn, data_dir, root)
    assert (where(conn, root, job) / "job_description.pdf").is_file()


def test_five_failed_builds_in_a_row_across_applications_stop_the_run(conn, data_dir, root, built):
    for title in ("One", "Two", "Three"):
        by_hand(conn, data_dir, title)
    built.fail = lambda number: True
    result = run(conn, data_dir, root)
    assert len(built) == 5 and result.stopped == "The PDF could not be built: a made-up reason."


def test_four_failed_builds_then_a_good_one_carry_on(conn, data_dir, root, built):
    for title in ("One", "Two", "Three"):
        by_hand(conn, data_dir, title)
    built.fail = lambda number: number != 5
    result = run(conn, data_dir, root)
    assert len(built) == 9 and result.stopped is None and len(result.problems) == 8


@pytest.mark.parametrize(
    ("column", "value"),
    [("fields", "[not json"), ("files", "[not json"), ("fields", '["a string"]'), ("files", '["a string"]'),
     ("fields", "null"), ("files", "null"), ("fields", '{"a": 1}'), ("files", "7")],
)  # fmt: skip
def test_a_damaged_record_is_a_line_and_the_rest_carries_on(conn, data_dir, root, built, column, value):
    job, other = by_hand(conn, data_dir, "Damaged"), by_hand(conn, data_dir, "Fine")
    with conn:
        conn.execute(f"UPDATE submissions SET {column} = ? WHERE job_id = ?", (value, job))  # noqa: S608 (test values)
    result = run(conn, data_dir, root)
    names = [path.name for path in where(conn, root, job).iterdir()]
    assert "status_history.txt" in names and "job_description.pdf" in names and "filled_answers.pdf" not in names
    assert result.problems and result.stopped is None
    assert sorted(path.name for path in where(conn, root, other).iterdir()) == ALL_FILES
    assert (where(conn, root, job) / "status_history.txt").read_text("utf-8").startswith("2026-10-17  Applied\n")


def test_an_unknown_way_of_applying_is_a_line(conn, data_dir, root, built, monkeypatch):
    job = by_hand(conn, data_dir)
    monkeypatch.delitem(record.HEADING, "by hand")
    result = run(conn, data_dir, root)
    assert len(result.problems) == 1 and not (where(conn, root, job) / "filled_answers.pdf").exists()


def test_a_paper_that_cant_be_printed_is_a_line_and_the_next_file_is_still_made(
    conn, data_dir, root, built, monkeypatch
):
    job = by_hand(conn, data_dir)
    monkeypatch.setattr(papers, "job_description", lambda _: ("Title", [("Heading", None)]))
    result = run(conn, data_dir, root)
    names = [path.name for path in where(conn, root, job).iterdir()]
    assert "job_description.pdf" not in names and "filled_answers.pdf" in names and len(result.problems) == 1


def test_a_date_that_isnt_real_is_a_line_and_no_folder(conn, data_dir, root, built):
    job, other = by_hand(conn, data_dir, "Damaged"), by_hand(conn, data_dir, "Fine")
    with conn:
        conn.execute("UPDATE submissions SET submitted_on = '../../x' WHERE job_id = ?", (job,))
    result = run(conn, data_dir, root)
    assert len(result.problems) == 1 and str(job) in result.problems[0] and where(conn, root, other).is_dir()
    assert conn.execute("SELECT count(*) FROM exported WHERE job_id = ?", (job,)).fetchone()[0] == 0


def test_a_record_of_a_job_no_longer_there_is_a_line(conn, data_dir, root, built):
    job, other = by_hand(conn, data_dir, "Gone"), by_hand(conn, data_dir, "Fine")
    with conn:
        conn.execute("UPDATE submissions SET job_id = 9999 WHERE job_id = ?", (job,))
    result = run(conn, data_dir, root)
    assert len(result.problems) == 1 and where(conn, root, other).is_dir() and result.stopped is None


def test_a_full_disk_stops_the_run_with_the_short_reason_and_keeps_what_is_done(
    conn, data_dir, root, built, monkeypatch
):
    first, second = by_hand(conn, data_dir, "One"), by_hand(conn, data_dir, "Two")
    run(conn, data_dir, root, ON, ON)
    tracker.move(conn, first, "applied", "offer", ON)
    (where(conn, root, second) / "resume.pdf").unlink()
    write = folder.write

    def full(target, data):
        raise OSError(28, "No space left on device", str(target))

    with monkeypatch.context() as disk:
        disk.setattr(folder, "write", full)
        result = run(conn, data_dir, root, ON, ON)
    assert write is folder.write and "No space left on device" in result.stopped and str(root) not in result.stopped
    assert "2" in result.stopped  # how far it got


def test_a_stopped_run_skips_the_refresh(conn, data_dir, root, built, monkeypatch):
    old, new = by_hand(conn, data_dir, "Old"), by_hand(conn, data_dir, "New", on=date(2026, 10, 20))
    run(conn, data_dir, root, ON, ON)
    tracker.move(conn, old, "applied", "offer", ON)
    before = (where(conn, root, old) / "status_history.txt").read_bytes()
    with monkeypatch.context() as disk:
        disk.setattr(folder, "write", lambda *_: (_ for _ in ()).throw(OSError(28, "No space left on device")))
        result = run(conn, data_dir, root, date(2026, 10, 20), None)
    assert result.stopped and (where(conn, root, old) / "status_history.txt").read_bytes() == before
    assert new


def test_tectonic_missing_stops_before_anything_is_made(conn, data_dir, root, built, monkeypatch):
    by_hand(conn, data_dir)
    monkeypatch.setattr(pdf, "tectonic", lambda: None)
    result = run(conn, data_dir, root)
    assert pdf.INSTALL in result.stopped and listing(root) == [] and rows(conn, "exported") == []


def test_no_folder_chosen_says_so(conn, data_dir, built):
    by_hand(conn, data_dir)
    assert run(conn, data_dir, "").stopped == folder.CHOOSE
    assert rows(conn, "exported") == []


def test_a_folder_that_isnt_there_is_never_made(conn, data_dir, root, built):
    by_hand(conn, data_dir)
    result = run(conn, data_dir, root / "unplugged")
    assert result.stopped == folder.GONE and not (root / "unplugged").exists() and rows(conn, "exported") == []


def test_a_folder_that_fails_the_check_at_run_time_stops_the_run(conn, data_dir, root, built):
    by_hand(conn, data_dir)
    (root / ".git").mkdir()
    assert run(conn, data_dir, root).stopped == folder.IN_GIT
    assert listing(root) == [".git"]


def test_a_folder_unplugged_during_the_run_stops_it_and_isnt_made_again(conn, data_dir, root, built, tmp_path):
    by_hand(conn, data_dir, "One")
    by_hand(conn, data_dir, "Two")

    def unplug(done, total):
        if done == 1:
            for path in sorted(root.rglob("*"), reverse=True):
                path.rmdir() if path.is_dir() else path.unlink()
            root.rmdir()

    result = run(conn, data_dir, root, step=unplug)
    assert result.stopped and not root.exists()


def test_a_job_with_no_text_is_said_once_and_text_added_later_makes_the_file(conn, data_dir, root, built):
    job = job_row(conn, description=None)
    tracker.move(conn, job, "ready", "applied", ON)
    first = run(conn, data_dir, root)
    assert not (where(conn, root, job) / "job_description.pdf").exists() and len(first.problems) == 1
    second = run(conn, data_dir, root)
    assert second.problems == [] and second.summary() == "Nothing new"
    with conn:
        conn.execute("UPDATE jobs SET description = 'Now we have text.' WHERE id = ?", (job,))
    third = run(conn, data_dir, root)
    assert third.problems == [] and third.files == 1 and (where(conn, root, job) / "job_description.pdf").is_file()


def test_a_missing_character_takes_a_second_build_with_the_closing_line(conn, data_dir, root, built):
    job = no_record(conn)
    built.missing = ("😀", "字")
    result = run(conn, data_dir, root)
    assert len(built) == 2 and "left out" not in built[0][1]
    assert "2 kinds of character could not be shown here and were left out." in built[1][1]
    assert b"were left out" in (where(conn, root, job) / "job_description.pdf").read_bytes()
    assert len(result.problems) == 1


def test_a_second_build_that_fails_makes_no_file_and_claims_nothing_left_out(conn, data_dir, root, built):
    job = no_record(conn)
    built.missing = ("😀",)
    built.fail = lambda number: number == 2
    result = run(conn, data_dir, root)
    assert not (where(conn, root, job) / "job_description.pdf").exists() and len(result.problems) == 1
    assert "left out" not in result.problems[0] and "a made-up reason" in result.problems[0]


def test_one_build_when_no_character_is_missing(conn, data_dir, root, built):
    no_record(conn)
    run(conn, data_dir, root)
    assert len(built) == 1


def test_the_builds_happen_in_the_data_folder_and_leave_nothing_there(conn, data_dir, root, built):
    by_hand(conn, data_dir)
    run(conn, data_dir, root)
    assert {target.parent for target, _ in built} == {data_dir / "documents" / "export"}
    assert list((data_dir / "documents" / "export").iterdir()) == []


def test_a_failed_build_leaves_nothing_in_the_chosen_folder_but_finished_files(conn, data_dir, root, built):
    by_hand(conn, data_dir)
    built.fail = lambda number: True
    run(conn, data_dir, root)
    assert not [name for name in listing(root) if "build" in name or name.endswith((".tex", ".log", ".json"))]


@pytest.mark.parametrize(
    ("company", "title"),
    [("../../x", "../../../etc/passwd"), ("CON", ""), ("x" * 500, "y" * 500), ("字节跳动", "机器学习"), ("🚀", "🤖"),
     (r"\input{/etc/passwd}", r"\write18{touch pwned}"), ("/", "\\"), (".", "..")],
)  # fmt: skip
def test_hostile_names_never_leave_the_chosen_folder(conn, data_dir, root, built, tmp_path, company, title):
    job = by_hand(conn, data_dir, title=title or "x", company=company)
    with conn:
        conn.execute("UPDATE jobs SET title = ? WHERE id = ?", (title, job))
        conn.execute("""UPDATE submissions SET fields = '[{"question": "\\\\input{x}", "value": "\\\\end{document}",
            "from": "you"}]'""")  # fmt: skip
    outside = sorted(path for path in tmp_path.rglob("*") if root not in path.parents and path != root)
    result = run(conn, data_dir, root)
    after = sorted(path for path in tmp_path.rglob("*") if root not in path.parents and path != root)
    assert not [path for path in after if path not in outside and "documents" not in path.parts]
    assert result.stopped is None and result.problems == []
    assert sorted(path.name for path in where(conn, root, job).iterdir()) == ALL_FILES
    assert all(source.count(r"\end{document}") == 1 and r"\input{" not in source for _, source in built)


def test_everything_made_is_yours_alone(conn, data_dir, root, built):
    by_hand(conn, data_dir)
    old = os.umask(0)
    try:
        run(conn, data_dir, root)
    finally:
        os.umask(old)
    assert listing(root)
    for path in root.rglob("*"):
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600), path.name


def test_step_counts_each_application(conn, data_dir, root, built):
    good = by_hand(conn, data_dir, "One")
    by_hand(conn, data_dir, "Two")
    with conn:
        conn.execute("UPDATE submissions SET files = '[not json' WHERE job_id = ?", (good,))
    seen = []
    run(conn, data_dir, root, step=lambda done, total: seen.append((done, total)))
    assert seen == [(0, 2), (1, 2), (2, 2)]


def test_no_name_path_or_answer_reaches_the_log(conn, data_dir, root, built, caplog):
    job = by_hand(conn, data_dir, company=SECRET)
    with conn:
        conn.execute("UPDATE submissions SET fields = ? WHERE job_id = ?",
                     (f'[{{"question": "{SECRET}", "value": "{SECRET}", "from": "you"}}]', job))  # fmt: skip
    built.fail = lambda number: number == 1
    with caplog.at_level(logging.DEBUG):
        run(conn, data_dir, root)
        with conn:
            conn.execute("UPDATE submissions SET files = '[not json'")
        run(conn, data_dir, root)
    assert SECRET not in caplog.text and str(root) not in caplog.text and "Sam" not in caplog.text
    assert caplog.records  # it does say something


def test_the_summary_counts_in_plain_words():
    found = export.Result(selected=20, folders=12, again=1, files=3, refreshed=5, beside=1)
    assert found.summary() == (
        "12 new folders, 1 folder made again, 3 files added, 5 status histories refreshed, "
        "1 status history left alone (it no longer matches: the new one is beside it)"
    )
    assert export.Result(selected=2, folders=1, files=2, refreshed=1, beside=2).summary() == (
        "1 new folder, 2 files added, 1 status history refreshed, "
        "2 status histories left alone (they no longer match: the new ones are beside them)"
    )


def test_the_summary_says_when_nothing_was_new_or_nothing_was_selected():
    assert export.Result(selected=3).summary() == "Nothing new"
    between = export.Result(selected=0, none_chosen=export.NONE_BETWEEN)
    assert between.summary() == "No applications between those dates"
    assert export.Result(selected=0, none_chosen=export.NONE_BETWEEN, refreshed=2).summary() == (
        "No applications between those dates; 2 status histories refreshed"
    )
    assert export.Result(selected=0).summary() == "Nothing new"


def test_a_run_stopped_before_it_chose_never_says_there_were_no_applications():
    assert export.Result(stopped="Choose a folder first").summary() == "Nothing new"


def test_the_summary_holds_no_names(conn, data_dir, root, built):
    by_hand(conn, data_dir, company=SECRET)
    assert SECRET not in run(conn, data_dir, root).summary()


def test_a_second_connection_sees_the_row_before_any_file_is_written(conn, data_dir, root, built, monkeypatch):
    """The row is committed, not only written: a crash can't lose it."""
    by_hand(conn, data_dir)
    write, seen = folder.write, []

    def look(target, data):
        with closing(db.connect(data_dir)) as other:
            seen.append(len(rows(other, "exported")))
        write(target, data)

    monkeypatch.setattr(folder, "write", look)
    run(conn, data_dir, root)
    assert seen and seen[0] == 1


def test_with_no_dates_and_nothing_to_export_it_says_so(conn, data_dir, root, built):
    assert run(conn, data_dir, root).summary() == "No applications to export yet"


def test_with_dates_and_nothing_in_them_it_says_so(conn, data_dir, root, built):
    by_hand(conn, data_dir)
    assert run(conn, data_dir, root, date(2026, 11, 1), None).summary() == "No applications between those dates"


def test_on_a_schedule_nothing_to_do_is_nothing_new(conn, data_dir, root, built):
    assert run(conn, data_dir, root, date(2026, 11, 1), None, unfinished=True).summary() == "Nothing new"


def test_a_run_stopped_with_nothing_done_says_the_reason_alone():
    assert export.Result(selected=4, stopped="Permission denied").words() == "Permission denied"
    assert export.Result(selected=4, files=1, stopped="Permission denied").words() == "1 file added. Permission denied"


@pytest.mark.parametrize(("count", "words"), [
    (1, "1 application not finished: the next export tries it again"),
    (2, "2 applications not finished: the next export tries them again"),
])  # fmt: skip
def test_what_isnt_finished_is_said_and_nothing_new_never_stands_alone(count, words):
    assert export.Result(selected=1, unfinished=count).summary() == words
    assert export.Result(selected=1, files=1, unfinished=count).summary() == f"1 file added, {words}"


def finished_on(conn, job):
    return conn.execute("SELECT finished_on FROM exported WHERE job_id = ?", (job,)).fetchone()[0]


def test_an_application_with_everything_made_is_finished_and_stays_so(conn, data_dir, root, built):
    job = by_hand(conn, data_dir)
    run(conn, data_dir, root)
    first = finished_on(conn, job)
    assert first
    with conn:
        conn.execute("UPDATE exported SET finished_on = 'kept' WHERE job_id = ?", (job,))
    run(conn, data_dir, root)
    assert finished_on(conn, job) == "kept"


def test_a_paper_that_fails_leaves_it_unfinished_and_a_later_failure_undoes_finished(conn, data_dir, root, built):
    job = by_hand(conn, data_dir)
    built.fail = lambda number: number == 1
    assert run(conn, data_dir, root).unfinished == 1 and finished_on(conn, job) is None
    built.fail = lambda number: False
    assert run(conn, data_dir, root).unfinished == 0 and finished_on(conn, job)
    (where(conn, root, job) / "job_description.pdf").unlink()
    built.fail = lambda number: True
    assert run(conn, data_dir, root).unfinished == 1 and finished_on(conn, job) is None


def test_no_text_and_a_gone_pdf_are_finished_as_no_later_run_could_help(conn, data_dir, root, built):
    no_text = job_row(conn, "No text", description=None)
    tracker.move(conn, no_text, "ready", "applied", ON)
    gone = by_hand(conn, data_dir, "Gone")
    pdf.tailored(data_dir, gone, 1).unlink()
    result = run(conn, data_dir, root)
    assert result.unfinished == 0 and finished_on(conn, no_text) and finished_on(conn, gone)
    assert export.applications(conn, date(2026, 11, 1), None, unfinished=True) == []


def test_a_damaged_record_and_a_refused_folder_are_unfinished(conn, data_dir, root, built):
    damaged, refused = by_hand(conn, data_dir, "Damaged"), by_hand(conn, data_dir, "Refused")
    with conn:
        conn.execute("UPDATE submissions SET fields = '[not json' WHERE job_id = ?", (damaged,))
        conn.execute("INSERT INTO exported (job_id, folder, exported_on, finished_on) VALUES (?, '../x', 'a', 'a')",
                     (refused,))  # fmt: skip
    result = run(conn, data_dir, root)
    assert result.unfinished == 2 and finished_on(conn, damaged) is None and finished_on(conn, refused) is None


def test_a_crash_leaves_finished_as_it_was(conn, data_dir, root, built, monkeypatch):
    job = by_hand(conn, data_dir)
    run(conn, data_dir, root)
    before = finished_on(conn, job)
    (where(conn, root, job) / "job_description.pdf").unlink()
    with monkeypatch.context() as crash:
        crash.setattr(folder, "write", lambda *_: (_ for _ in ()).throw(RuntimeError("the power went")))
        with pytest.raises(RuntimeError):
            run(conn, data_dir, root)
    assert finished_on(conn, job) == before


def test_what_failed_before_goes_last_so_a_new_job_always_gets_through(conn, data_dir, root, built):
    stuck = [by_hand(conn, data_dir, name, on=date(2026, 1, day)) for day, name in ((1, "Stuck one"), (2, "Stuck two"))]
    built.refused = ("Stuck one", "Stuck two")
    run(conn, data_dir, root)
    new = by_hand(conn, data_dir, "New job", on=date(2026, 10, 1))
    assert [job for job, _ in export.applications(conn, None, None)] == [new, *stuck]
    assert [job for job, _ in export.applications(conn, date(2026, 11, 1), None, unfinished=True)] == [new, *stuck]
    run(conn, data_dir, root, date(2026, 11, 1), None, unfinished=True)
    assert finished_on(conn, new) and (where(conn, root, new) / "filled_answers.pdf").is_file()


def test_a_card_moved_back_isnt_counted_as_unfinished_for_ever(conn, data_dir, root, built):
    job = no_record(conn)
    built.fail = lambda number: True
    assert run(conn, data_dir, root).unfinished == 1
    tracker.move(conn, job, "applied", "ready", ON)
    assert run(conn, data_dir, root).unfinished == 0


def test_the_refresh_never_touches_finished(conn, data_dir, root, built):
    job = by_hand(conn, data_dir)
    run(conn, data_dir, root)
    with conn:
        conn.execute("UPDATE exported SET finished_on = 'kept'")
    tracker.move(conn, job, "applied", "offer", ON)
    assert run(conn, data_dir, root, date(2026, 11, 1), None).refreshed == 1 and finished_on(conn, job) == "kept"


def test_a_file_that_appears_during_a_build_is_left_and_not_counted(conn, data_dir, root, built):
    job = by_hand(conn, data_dir)
    run(conn, data_dir, root)
    place = where(conn, root, job)
    (place / "job_description.pdf").unlink()
    built.during = lambda target: (place / "job_description.pdf").write_bytes(b"yours")
    result = run(conn, data_dir, root)
    assert (place / "job_description.pdf").read_bytes() == b"yours" and result.files == 0
    assert result.summary() == "Nothing new" and not list(root.rglob("*.partial"))


def test_a_file_that_appears_during_a_copy_is_left_and_not_counted(conn, data_dir, root, built, monkeypatch):
    job = by_hand(conn, data_dir)
    run(conn, data_dir, root)
    target = where(conn, root, job) / "resume.pdf"
    target.unlink()
    read = Path.read_bytes

    def appears(path):
        data = read(path)
        if path == pdf.tailored(data_dir, job, 1):
            target.write_bytes(b"yours")
        return data

    monkeypatch.setattr(Path, "read_bytes", appears)
    result = run(conn, data_dir, root)
    assert read(target) == b"yours" and result.files == 0


def test_an_odd_item_is_a_line_with_the_reason_and_the_next_application_goes_on(conn, data_dir, root, built):
    odd, fine = by_hand(conn, data_dir, "Odd"), by_hand(conn, data_dir, "Fine")
    run(conn, data_dir, root)
    place = where(conn, root, odd)
    (place / "resume.pdf").unlink()
    (where(conn, root, fine) / "resume.pdf").unlink()
    (place / "resume.pdf.partial").mkdir()
    result = run(conn, data_dir, root)
    (line,) = result.problems
    assert result.stopped is None and "Acme, Odd" in line and "File exists" in line and str(root) not in line
    assert result.unfinished == 1 and (where(conn, root, fine) / "resume.pdf").is_file()
    assert finished_on(conn, odd) is None and finished_on(conn, fine)


def test_a_history_closed_to_you_is_a_line_not_a_stop(conn, data_dir, root, built):
    closed, fine = by_hand(conn, data_dir, "Closed"), by_hand(conn, data_dir, "Fine")
    run(conn, data_dir, root)
    history = where(conn, root, closed) / "status_history.txt"
    tracker.move(conn, fine, "applied", "offer", ON)
    tracker.move(conn, closed, "applied", "offer", ON)
    history.chmod(0)
    try:
        result = run(conn, data_dir, root, date(2026, 11, 1), None)
    finally:
        history.chmod(0o600)
    assert result.stopped is None and result.refreshed == 1
    assert len(result.problems) == 1 and "Permission denied" in result.problems[0]


def test_a_whole_folder_closed_to_you_stops_with_one_message_not_hundreds(conn, data_dir, root, built):
    for number in range(8):
        by_hand(conn, data_dir, f"Job {number}")
    root.chmod(0o500)
    try:
        result = run(conn, data_dir, root)
    finally:
        root.chmod(0o700)
    assert "Permission denied" in result.stopped and str(root) not in result.stopped and len(result.problems) == 5


def test_a_full_disk_still_stops_the_run_at_once(conn, data_dir, root, built, monkeypatch):
    by_hand(conn, data_dir, "One")
    by_hand(conn, data_dir, "Two")
    monkeypatch.setattr(folder, "write", lambda *_: (_ for _ in ()).throw(OSError(28, "No space left on device")))
    result = run(conn, data_dir, root)
    assert result.stopped.startswith("No space left on device") and result.problems == []


def exported_jobs(conn, data_dir, root, count):
    """Applications on days in a row, all their files made, so a run has nothing to build."""
    jobs = [by_hand(conn, data_dir, f"Job {day}", on=date(2026, 10, day)) for day in range(1, count + 1)]
    run(conn, data_dir, root)
    return jobs


def in_the_way(conn, root, job):
    place = where(conn, root, job)
    (place / "resume.pdf").unlink()
    (place / "resume.pdf.partial").mkdir()


def test_five_odd_items_with_fine_applications_between_are_not_in_a_row(conn, data_dir, root, built):
    jobs = exported_jobs(conn, data_dir, root, 10)
    for job in jobs[::2]:
        in_the_way(conn, root, job)
    built.clear()
    result = run(conn, data_dir, root)
    assert result.stopped is None and len(result.problems) == 5 and result.unfinished == 5 and built == []


def test_five_odd_items_in_a_row_stop_the_run(conn, data_dir, root, built):
    jobs = exported_jobs(conn, data_dir, root, 6)
    for job in jobs[:5]:
        in_the_way(conn, root, job)
    assert "File exists" in run(conn, data_dir, root).stopped
