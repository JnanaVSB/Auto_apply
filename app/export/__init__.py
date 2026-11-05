"""The local archive: your applications as plain files in a folder you choose. Never online, never an AI. A run only
reads the records, adds what is missing and leaves whatever is there; one file that fails doesn't stop an application,
and `exported` remembers each folder before any file is written, so a run stopped anywhere carries on with no duplicate.
Logs hold counts and kinds only: the problem lines naming an application are for the page."""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
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
DOCUMENTS = {runs.KIND: "resume.pdf", runs.LETTER: "cover_letter.pdf"}
ANSWERS, POSTING, NOTES = "filled_answers.pdf", "job_description.pdf", "council_notes.pdf"
STOP_AFTER = 5  # failed builds in a row: a broken Tectonic gives one message, not hundreds
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
    problems: list[str] = field(default_factory=list)  # each names an application: for the page only, never saved
    stopped: str | None = None

    def summary(self) -> str:
        """Counts only, in plain words: this is what is saved."""
        counts = [
            _count(self.folders, "new folder", "new folders"),
            _count(self.again, "folder made again", "folders made again"),
            _count(self.files, "file added", "files added"),
            _count(self.refreshed, "status history refreshed", "status histories refreshed"),
            _count(self.beside, "status history left alone (it no longer matches: the new one is beside it)",
                   "status histories left alone (they no longer match: the new ones are beside them)"),
        ]  # fmt: skip
        words = ", ".join(filter(None, counts))
        if self.selected == 0:
            return "; ".join(filter(None, ("No applications between those dates", words)))
        return words or "Nothing new"


class _Stopped(Exception):  # noqa: N818 (the run ends here, with its reason)
    pass


def which(conn: sqlcipher3.Connection, start: date | None, end: date | None, unexported: bool = False) -> list:
    """The applications to export, as (job, day), oldest first: every record on the date you chose, wherever its card
    is now, and cards past Ready with no record on the card's date. `unexported`: also all not exported yet, whatever
    their date."""
    first, last = start and start.isoformat(), end and end.isoformat()
    kept = {row[0] for row in conn.execute("SELECT job_id FROM exported")}
    found = sorted((day, job) for job, day in conn.execute(APPLIED))
    return [
        (job, day)
        for day, job in found
        if (unexported and job not in kept) or ((not first or first <= day) and (not last or day <= last))
    ]


def run(
    conn: sqlcipher3.Connection, data_dir: Path, text: str, start: date | None, end: date | None,
    unexported: bool = False, step: Callable[[int, int], None] | None = None,
) -> Result:  # fmt: skip
    """One export to the folder `text`, then the status history of every other application exported before. `step`
    hears (done, total). Disk full or no rights stop it with the system's reason; what's done stays."""
    result = Result()
    try:
        root = folder.ready(text, data_dir)
        if pdf.tectonic() is None:
            raise _Stopped(f"Tectonic isn't installed: install it with: {pdf.INSTALL}")
        chosen = which(conn, start, end, unexported)
        result.selected = len(chosen)
        _Run(conn, data_dir, root, result).all_of(chosen, step or (lambda done, total: None))
    except (folder.Refused, _Stopped) as reason:
        result.stopped = str(reason)
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
    failed: int = 0  # builds in a row
    seen: set[int] = field(default_factory=set)

    def all_of(self, chosen: list, step: Callable[[int, int], None]) -> None:
        done = 0
        step(done, len(chosen))
        try:
            for job_id, day in chosen:
                self.seen.add(job_id)
                self._safely(self._application, job_id, day, name=f"Application {job_id}", file="its details")
                done += 1
                step(done, len(chosen))
            self._refresh()
        except OSError as error:
            raise _Stopped(f"{error.strerror or type(error).__name__}: stopped after {done} of {len(chosen)} "
                           "applications; what's done stays") from None  # fmt: skip

    def _application(self, job_id: int, day: str) -> None:
        job = self._job(job_id)
        found, damaged = self._record(job_id)
        where, new = self._place(job_id, day, job)
        if where is None:
            return
        name = _name(job)
        if damaged:
            self._problem(name, "its record can't be read, so its answers and files are left out")
        elif found:
            self._safely(self._recorded_copies, where, job_id, found, name, name=name, file="its files")
        else:
            self._newest(where, job_id, name)
        self._safely(self._history, where, job_id, found, name=name, file=folder.HISTORY)
        absent = "the app holds no text for this job" if new else None  # said once: a schedule would repeat it for ever
        self._paper(where, POSTING, name, lambda: papers.job_description(job), absent)
        if not damaged:
            self._paper(
                where, NOTES, name, lambda: papers.council_notes(self.conn, job_id, self._versions(job_id, found))
            )
        if found and not damaged:
            self._paper(where, ANSWERS, name, lambda: self._answers(where, job_id, job, found, name))

    def _job(self, job_id: int) -> dict:
        row = self.conn.execute(JOB, (job_id,)).fetchone()
        if row is None:
            raise KeyError("no job")
        return dict(zip(("company", "title", "locations", "apply_url", "description"), row, strict=True))

    def _record(self, job_id: int) -> tuple[dict | None, bool]:
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
            day = date.fromisoformat(day).isoformat()
            taken = [found[0] for found in self.conn.execute("SELECT folder FROM exported")]
            relative = folder.free(self.root, day, folder.name(job["company"], job["title"]), taken)
            with self.conn:
                self.conn.execute(SAVE, (job_id, relative, collect.now()))
        else:
            relative = row[0]
        gone = folder.missing(self.root / relative)
        try:
            where = folder.enter(self.root, relative)
        except folder.Refused as refusal:
            self._problem(_name(job), str(refusal))
            return None, False
        self.result.folders += row is None
        self.result.again += gone and row is not None
        return where, row is None

    def _recorded_copies(self, where: Path, job_id: int, found: dict, name: str) -> None:
        for row in found["files"]:
            self._safely(self._recorded_copy, where, job_id, row, name=name, file="a recorded file")

    def _recorded_copy(self, where: Path, job_id: int, row: dict) -> None:
        """The very version the record names, or nothing: never the newest instead, never built again."""
        target = where / DOCUMENTS[row["slot"]]
        source = pdf.tailored(self.data_dir, job_id, row["version"], row["slot"])
        if folder.missing(target) and source.is_file():
            folder.copy(source, target)
            self.result.files += 1

    def _newest(self, where: Path, job_id: int, name: str) -> None:
        for kind, document in record.documents(self.conn, self.data_dir, job_id).items():
            target = where / DOCUMENTS[kind]
            if document is None or not folder.missing(target):
                continue
            if document[1].stat().st_size == 0:
                self._problem(name, f"{target.name}: the app's copy is empty, so it was left out")
                continue
            folder.copy(document[1], target)
            self.result.files += 1

    def _history(self, where: Path, job_id: int, found: dict | None) -> None:
        happened = folder.history(where, papers.history(self.conn, job_id, found), self.today)
        self.result.files += happened == "made"
        self.result.refreshed += happened == "refreshed"
        self.result.beside += happened == "beside"
        if happened == "beside":
            job = self._job(job_id)
            self._problem(_name(job), "its status history no longer matches: the new one is beside it")

    def _versions(self, job_id: int, found: dict | None) -> list:
        """What the record names, else the newest finished ones."""
        if found:
            return [(row["label"], runs.latest(self.conn, job_id, row["slot"], runs.NUMBERED, row["version"]))
                    for row in found["files"]]  # fmt: skip
        versions = [(name, runs.last_done(self.conn, job_id, kind)) for kind, name in record.DOCUMENTS]
        return [(f"{name} v{version['version']}" if version else name, version) for name, version in versions]

    def _answers(self, where: Path, job_id: int, job: dict, found: dict, name: str) -> tuple:
        """The record's paper, saying what is true of the copies in the folder now, not of the app's own files."""
        states = {row["slot"]: _state(where / DOCUMENTS[row["slot"]], row["sha256"])
                  for row in found["files"] if row["slot"] in DOCUMENTS}  # fmt: skip
        back = record.moved_back(self.conn, job_id, found)
        paper = papers.filled_answers(job, found, back and papers.day(back), states)
        words = {"gone": record.MISSING, "different": papers.NOT_RECORDED}
        for slot, state in states.items():
            if state in words:
                self._problem(name, f"{DOCUMENTS[slot]}: {words[state]}")
        return paper

    def _paper(self, where: Path, file: str, name: str, make: Callable, absent: str | None = None) -> None:
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
            if built:
                folder.copy(work, target)
                self.result.files += 1
        finally:
            work.unlink(missing_ok=True)
            work.with_suffix(".json").unlink(missing_ok=True)

    def _build(self, work: Path, name: str, file: str, source: str) -> pdf.Built | None:
        try:
            built = pdf.build_kept(self.data_dir, work, source)
        except pdf.PdfError as error:
            self.failed += 1
            self._problem(name, f"{file}: {error}")
            if self.failed >= STOP_AFTER:
                raise _Stopped(str(error)) from None
            return None
        self.failed = 0
        return built

    def _refresh(self) -> None:
        """Every other application's status history, whatever its date: only that text file, and never a folder or a
        file you deleted."""
        for job_id, relative in self.conn.execute("SELECT job_id, folder FROM exported").fetchall():
            if job_id not in self.seen and not folder.missing(self.root / relative / folder.HISTORY):
                self._safely(self._refreshed, job_id, relative, name=f"Application {job_id}", file=folder.HISTORY)

    def _refreshed(self, job_id: int, relative: str) -> None:
        try:
            where = folder.enter(self.root, relative)
        except folder.Refused as refusal:
            self._problem(_name(self._job(job_id)), str(refusal))
            return
        self._history(where, job_id, self._record(job_id)[0])

    def _safely(self, work: Callable, *values: object, name: str, file: str) -> None:
        """One file or application that can't be read as it should is a line, and the rest carries on."""
        try:
            work(*values)
        except DAMAGED as error:
            self._damaged(name, file, error)

    def _damaged(self, name: str, file: str, error: Exception) -> None:
        log.warning("Export: damaged data skipped: %s", type(error).__name__)
        self._problem(name, f"{file}: its saved data is damaged, so this was left out")

    def _problem(self, name: str, words: str) -> None:
        self.result.problems.append(f"{name}: {words}")


def _name(job: dict) -> str:
    return f"{job['company']}, {job['title']}"


def _state(target: Path, recorded: str) -> str:
    if folder.missing(target):
        return "gone"
    return "same" if folder.digest(target) == recorded else "different"


def _count(number: int, one: str, many: str) -> str:
    if not number:
        return ""
    return f"{number} {one if number == 1 else many}"
