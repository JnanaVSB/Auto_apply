"""The local archive: your applications as plain files in a folder you choose. Never online, never an AI. A run only
reads the records, adds what is missing and leaves whatever is there; one file that fails doesn't stop an application,
and `exported` remembers each folder before any file is written, so a run stopped anywhere carries on with no duplicate.
Logs hold counts and kinds only: the problem lines naming an application are for the page."""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import sqlcipher3

from app import collect
from app.council import runs
from app.documents import latex, pdf
from app.export import folder, papers
from app.submit import record

APPLIED = """SELECT job_id, submitted_on FROM submissions UNION ALL SELECT job_id, applied_on FROM tracker
    WHERE stage != 'ready' AND job_id NOT IN (SELECT job_id FROM submissions)"""
JOB = "SELECT company, title, locations, apply_url, description FROM jobs WHERE id = ?"
SAVE = "INSERT INTO exported (job_id, folder, exported_on) VALUES (?, ?, ?)"
FINISHED = "UPDATE exported SET finished_on = ? WHERE job_id = ? AND finished_on IS NULL"
NOT_FINISHED = "UPDATE exported SET finished_on = NULL WHERE job_id = ?"
LEFT = f"SELECT count(*) FROM exported WHERE finished_on IS NULL AND job_id IN (SELECT job_id FROM ({APPLIED}))"  # noqa: S608
DOCUMENT_FILES = {runs.KIND: "resume.pdf", runs.LETTER: "cover_letter.pdf"}
ANSWERS_PDF, POSTING_PDF, NOTES_PDF = "filled_answers.pdf", "job_description.pdf", "council_notes.pdf"
NEXT_RUN = "the next scheduled run or an Export"
NONE_BETWEEN, NONE_YET = "No applications between those dates", "No applications to export yet"
STOP_AFTER = 5  # failures in a row: a broken Tectonic or a closed folder gives one message, not hundreds
ODD_ITEM = (
    FileExistsError,
    PermissionError,
    IsADirectoryError,
    NotADirectoryError,
)  # one thing in the way, not the disk
DAMAGED = (ValueError, KeyError, TypeError, AttributeError)  # a record or a paper that can't be read as it should
log = logging.getLogger("app.export")


@dataclass
class Result:
    selected: int | None = None  # None: stopped before choosing any
    folders: int = 0
    again: int = 0  # folders made again: deleted, or a new chosen folder
    files: int = 0
    refreshed: int = 0
    beside: int = 0
    unfinished: int = 0  # the next scheduled run, or an Export over their dates, tries them again
    none_chosen: str = ""  # the words when none were chosen; on a schedule, none needed
    problems: list[str] = field(default_factory=list)  # each names an application: for the page only, never saved
    stopped: str | None = None

    def summary(self) -> str:
        """Counts only, in plain words."""
        words = self._counts()
        if self.selected == 0 and self.none_chosen:
            return "; ".join(filter(None, (self.none_chosen, words)))
        return words or "Nothing new"

    def words(self) -> str:
        """The run's last words, for the page and the database: the summary and why it stopped, or only why, when
        nothing was done."""
        return ". ".join(filter(None, (self._counts(), self.stopped))) if self.stopped else self.summary()

    def _counts(self) -> str:
        return ", ".join(filter(None, (
            _count(self.folders, "new folder", "new folders"),
            _count(self.again, "folder made again", "folders made again"),
            _count(self.files, "file added", "files added"),
            _count(self.refreshed, "status history refreshed", "status histories refreshed"),
            _count(self.beside, "status history left alone (it no longer matches: the new one is beside it)",
                   "status histories left alone (they no longer match: the new ones are beside them)"),
            _count(self.unfinished, f"application not finished: {NEXT_RUN} over its date tries it again",
                   f"applications not finished: {NEXT_RUN} over their dates tries them again"),
        )))  # fmt: skip


class _Stopped(Exception):  # noqa: N818 (the run ends here, with its reason)
    pass


def applications(
    conn: sqlcipher3.Connection, start: date | None, end: date | None, unfinished: bool = False
) -> list[tuple[int, str]]:
    """The applications to export, as (job, day), oldest first: every record on the date you chose, wherever its card
    is now, and cards past Ready with no record on the card's date. `unfinished`: also all not exported yet or left
    half-made, whatever their date. What failed before goes last, so that it can never hold up a new one."""
    first, last = start and start.isoformat(), end and end.isoformat()
    kept = dict(conn.execute("SELECT job_id, finished_on FROM exported").fetchall())
    found = sorted((job in kept and kept[job] is None, day, job) for job, day in conn.execute(APPLIED))
    return [
        (job, day)
        for again, day, job in found
        if (unfinished and (again or job not in kept)) or ((not first or first <= day) and (not last or day <= last))
    ]


def since(conn: sqlcipher3.Connection, every_days: int) -> date:
    """A schedule's first date: the set days back, or the last good run if that is earlier, so a dashboard closed for
    three weeks loses nothing."""
    back = date.today() - timedelta(days=every_days)
    row = conn.execute("SELECT good_on FROM export_runs WHERE id = 1").fetchone()
    return min(back, date.fromisoformat(papers.day(row[0]))) if row and row[0] else back


def run(
    conn: sqlcipher3.Connection, data_dir: Path, folder_text: str, start: date | None, end: date | None,
    step: Callable[[int, int], None], unfinished: bool = False,
) -> Result:  # fmt: skip
    """One export to the folder `folder_text`, then the status history of every other application exported before.
    `step` hears (done, total). Disk full or no rights stop it with the system's reason; what's done stays."""
    result = Result()
    try:
        root = folder.ready(folder_text, data_dir)
        if pdf.tectonic() is None:
            raise _Stopped(f"Tectonic isn't installed: install it with: {pdf.INSTALL}")
        chosen = applications(conn, start, end, unfinished)
        result.selected = len(chosen)
        result.none_chosen = "" if unfinished else NONE_BETWEEN if start or end else NONE_YET
        _Run(conn, data_dir, root, result).export_all(chosen, step)
    except (folder.Refused, _Stopped) as reason:
        result.stopped = str(reason)
    if result.selected is not None:
        result.unfinished = conn.execute(LEFT).fetchone()[0]
    log.info("Export: %d applications, %d new folders, %d files added, %d problems, stopped: %s", result.selected or 0,
             result.folders, result.files, len(result.problems), result.stopped is not None)  # fmt: skip
    return result


@dataclass
class _Run:
    conn: sqlcipher3.Connection
    data_dir: Path
    root: Path
    result: Result
    today: str = field(default_factory=lambda: date.today().isoformat())
    failed: int = 0  # builds and odd items in a row
    again: bool = False  # the application at hand has something a later run could put right

    def export_all(self, chosen: list[tuple[int, str]], step: Callable[[int, int], None]) -> None:
        """Each application, then every other status history. Failures in a row stop the applications but not the
        histories, which need no build; a stop of the disk ends everything. The first reason is the one said."""
        visited: set[int] = set()
        done, stopped = 0, None
        step(done, len(chosen))
        try:
            try:
                for job_id, day in chosen:
                    visited.add(job_id)
                    self._one(job_id, day)
                    done += 1
                    step(done, len(chosen))
            except _Stopped as reason:
                stopped, self.failed = reason, 0
            try:
                self._refresh(visited)
            except _Stopped as reason:
                stopped = stopped or reason
        except OSError as error:
            reason = error.strerror or type(error).__name__
            raise stopped or _Stopped(
                f"{reason}: stopped after {done} of {len(chosen)} applications; what's done stays"
            ) from None
        if stopped:
            raise stopped

    def _one(self, job_id: int, day: str) -> None:
        self.again = False
        try:
            self._safely(self._application, job_id, day, name=f"Application {job_id}", file="its details")
        except _Stopped:
            self._finish(job_id)  # the one a stop lands on is as unfinished as the ones before it
            raise
        self._finish(job_id)

    def _finish(self, job_id: int) -> None:
        """Only here, at the end of an application: a run stopped or closed earlier leaves things as they were."""
        with self.conn:
            if self.again:
                self.conn.execute(NOT_FINISHED, (job_id,))
            else:
                self.failed = 0
                self.conn.execute(FINISHED, (collect.now(), job_id))

    def _application(self, job_id: int, day: str) -> None:
        job = self._job(job_id)
        name = _name(job)
        try:
            self._files(job_id, day, job, name)
        except ODD_ITEM as error:
            self._odd_item(name, error)

    def _files(self, job_id: int, day: str, job: dict, name: str) -> None:
        recorded, damaged = self._read_record(job_id)
        where, new = self._place(job_id, day, job)
        if where is None:
            return
        if damaged:
            self.again = True
            self._problem(name, "its record can't be read, so its answers and files are left out")
        elif recorded:
            self._safely(self._recorded_copies, where, job_id, recorded, name, name=name, file="its files")
        else:
            self._newest(where, job_id, name)
        self._safely(self._history, where, job_id, recorded, name, name=name, file=folder.HISTORY)
        absent = "the app holds no text for this job" if new else None  # said once: a schedule would repeat it for ever
        self._paper(where, POSTING_PDF, name, lambda: papers.job_description(job), absent)
        if not damaged:
            self._paper(
                where,
                NOTES_PDF,
                name,
                lambda: papers.council_notes(self.conn, job_id, self._versions(job_id, recorded)),
            )
        if recorded and not damaged:
            self._paper(where, ANSWERS_PDF, name, lambda: self._answers(where, job_id, job, recorded, name))

    def _job(self, job_id: int) -> dict:
        row = self.conn.execute(JOB, (job_id,)).fetchone()
        if row is None:
            raise KeyError("no job")
        return dict(zip(("company", "title", "locations", "apply_url", "description"), row, strict=True))

    def _read_record(self, job_id: int) -> tuple[dict | None, bool]:
        """The record, and whether it is damaged: then it holds only the date you chose, for the status history."""
        try:
            return record.recorded(self.conn, job_id), False
        except DAMAGED:
            day = self.conn.execute("SELECT submitted_on FROM submissions WHERE job_id = ?", (job_id,)).fetchone()[0]
            return {"submitted_on": day}, True

    def _place(self, job_id: int, day: str, job: dict) -> tuple[Path | None, bool]:
        """The application's folder, made if needed, and whether it is new. A new one is remembered first, committed,
        so a crash can't make a second one, and whatever happens to the job's title a later run finds it."""
        row = self.conn.execute("SELECT folder FROM exported WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            day = date.fromisoformat(day).isoformat()  # a date that isn't real stops here, before it names a folder
            taken = [saved for (saved,) in self.conn.execute("SELECT folder FROM exported")]
            relative = folder.free(self.root, day, folder.name(job["company"], job["title"]), taken)
            with self.conn:
                self.conn.execute(SAVE, (job_id, relative, collect.now()))
        else:
            relative = row[0]
        gone = folder.missing(self.root / relative)
        try:
            where = folder.enter(self.root, relative)
        except folder.Refused as refusal:
            self.again = True
            self._problem(_name(job), str(refusal))
            return None, False
        if row is None:
            self.result.folders += 1
        elif gone:
            self.result.again += 1
        return where, row is None

    def _recorded_copies(self, where: Path, job_id: int, recorded: dict, name: str) -> None:
        for row in recorded["files"]:
            self._safely(self._recorded_copy, where, job_id, row, name=name, file="a recorded file")

    def _recorded_copy(self, where: Path, job_id: int, row: dict) -> None:
        """The very version the record names, or nothing: never the newest instead, never built again."""
        target = where / DOCUMENT_FILES[row["slot"]]
        source = pdf.tailored(self.data_dir, job_id, row["version"], row["slot"])
        if folder.missing(target) and source.is_file() and folder.write(target, source.read_bytes()):
            self.result.files += 1

    def _newest(self, where: Path, job_id: int, name: str) -> None:
        for kind, document in record.documents(self.conn, self.data_dir, job_id).items():
            target = where / DOCUMENT_FILES[kind]
            if document is None or not folder.missing(target):
                continue
            if document[1].stat().st_size == 0:
                self._problem(name, f"{target.name}: the app's copy is empty, so it was left out")
                continue
            if folder.write(target, document[1].read_bytes()):
                self.result.files += 1

    def _history(self, where: Path, job_id: int, recorded: dict | None, name: str) -> None:
        happened = folder.history(where, papers.history(self.conn, job_id, recorded), self.today)
        if happened == "made":
            self.result.files += 1
        elif happened == "refreshed":
            self.result.refreshed += 1
        elif happened == "beside":
            self.result.beside += 1
            self._problem(name, "its status history no longer matches: the new one is beside it")

    def _versions(self, job_id: int, recorded: dict | None) -> list[tuple[str, dict | None]]:
        """What the record names, else the newest finished ones."""
        if recorded:
            return [(row["label"], runs.latest(self.conn, job_id, row["slot"], runs.NUMBERED, row["version"]))
                    for row in recorded["files"]]  # fmt: skip
        versions = [(name, runs.last_done(self.conn, job_id, kind)) for kind, name in record.DOCUMENTS]
        return [(f"{name} v{version['version']}", version) for name, version in versions if version]

    def _answers(self, where: Path, job_id: int, job: dict, recorded: dict, name: str) -> tuple:
        """The record's paper, saying what is true of the copies in the folder now, not of the app's own files."""
        states = {row["slot"]: _state(where / DOCUMENT_FILES[row["slot"]], row["sha256"])
                  for row in recorded["files"] if row["slot"] in DOCUMENT_FILES}  # fmt: skip
        back = record.moved_back(self.conn, job_id, recorded)
        paper = papers.filled_answers(job, recorded, back and papers.day(back), states)
        for row in recorded["files"]:
            if row["slot"] in states and states[row["slot"]] != "same":
                notes = "; ".join(papers.file_notes(row, states[row["slot"]]))
                self._problem(name, f"{DOCUMENT_FILES[row['slot']]}: {notes}")
        return paper

    def _paper(
        self, where: Path, file: str, name: str, make: Callable[[], tuple | None], absent: str | None = None
    ) -> None:
        """A paper that isn't there yet: built in the data folder, so that only a finished PDF reaches yours."""
        target = where / file
        if not folder.missing(target):
            return
        try:
            paper = make()
            source = paper and latex.report(*paper)
        except DAMAGED as error:
            self._damaged(name, file, error)
            return
        if paper is None:
            if absent:
                self._problem(name, f"{file}: {absent}")
            return
        work = self.data_dir / "documents" / "export" / "paper.pdf"
        try:
            built = self._build(work, name, file, source)
            if built and built.missing:
                built = self._build(work, name, file, latex.report(*paper, left_out=len(built.missing)))
                if built:
                    self._problem(name, f"{file}: some characters could not be shown and were left out")
            if built and folder.write(target, work.read_bytes()):
                self.result.files += 1
        finally:
            work.unlink(missing_ok=True)
            work.with_suffix(".json").unlink(missing_ok=True)

    def _build(self, work: Path, name: str, file: str, source: str) -> pdf.Built | None:
        try:
            built = pdf.build_kept(self.data_dir, work, source)
        except pdf.PdfError as error:
            self._failed(name, f"{file}: {error}", str(error))
            return None
        self.failed = 0
        return built

    def _odd_item(self, name: str, error: OSError) -> None:
        """One thing in the way, such as a file closed to you: this application waits and the next one goes on."""
        reason = error.strerror or type(error).__name__
        self._failed(
            name,
            f"{reason}: {NEXT_RUN} over its date tries this application again",
            f"{reason}: check the export folder",
        )

    def _failed(self, name: str, line: str, reason: str) -> None:
        self.again = True
        self.failed += 1
        self._problem(name, line)
        if self.failed >= STOP_AFTER:
            raise _Stopped(reason)

    def _refresh(self, seen: set[int]) -> None:
        """Every other application's status history, whatever its date: only that text file, and never a folder or a
        file you deleted."""
        for job_id, relative in self.conn.execute("SELECT job_id, folder FROM exported").fetchall():
            if job_id not in seen and not folder.missing(self.root / relative / folder.HISTORY):
                self._safely(self._refresh_one, job_id, relative, name=f"Application {job_id}", file=folder.HISTORY)

    def _refresh_one(self, job_id: int, relative: str) -> None:
        name = _name(self._job(job_id))
        try:
            self._history(folder.enter(self.root, relative), job_id, self._read_record(job_id)[0], name)
            self.failed = 0
        except folder.Refused as refusal:
            self._problem(name, str(refusal))
        except ODD_ITEM as error:
            self._odd_item(name, error)

    def _safely(self, work: Callable[..., None], *values: object, name: str, file: str) -> None:
        """One file or application that can't be read as it should is a line, and the rest carries on."""
        try:
            work(*values)
        except DAMAGED as error:
            self._damaged(name, file, error)

    def _damaged(self, name: str, file: str, error: Exception) -> None:
        self.again = True
        log.warning("Export: damaged data skipped: %s", type(error).__name__)
        self._problem(name, f"{file}: its saved data is damaged, so this was left out")

    def _problem(self, name: str, words: str) -> None:
        self.result.problems.append(f"{name}: {words}")


def _name(job: dict) -> str:
    return f"{job['company']}, {job['title']}"


def _state(target: Path, sha256: str) -> str:
    if folder.missing(target):
        return "gone"
    return "same" if folder.digest(target) == sha256 else "different"


def _count(number: int, one: str, many: str) -> str:
    if not number:
        return ""
    return f"{number} {one if number == 1 else many}"
