"""A fill (Phase 7): one press of Open and fill or the plugin's toolbar. Here are the form's address, the rules for a
fill, the `fills` row that counts it (what the daily cap and the pause read), its ticket and the one-time pass a browser
needs to start. Tickets live in memory: stopping the dashboard ends them all, and the rows stay. fill_words.py says
the rows in plain words."""

import hashlib
import json
import math
import secrets
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import sqlcipher3

from app.collect import now
from app.config import Config
from app.council import runs
from app.resume import CONTROL
from app.sources import closed_site, is_simplify_site, posting_of
from app.sources.boards import BOARDS
from app.submit import MAX_FIELDS, Form, ashby, compare, greenhouse, lever, preview, record, save
from app.submit.fill_words import (
    BROWSER_REMOVED,
    CHANGED_FILE,
    GONE,
    LEFT,
    LOGGED_OUT,
    NOT_SENT,
    NOTHING_TO_FILL,
    REPLACED,
    SAME_FILE,
    SUBMITTED,
    THANKED,
    TOO_MANY_FIELDS,
    UNEXPECTED_PAGE,
)

SITES = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}
UNUSED_SECONDS = 5 * 60  # a ticket no browser has taken
RUNNING_SECONDS = 2 * 60 * 60  # a taken ticket
THANKS_SECONDS = 2 * 60  # how long after your press of Submit a thank-you page counts
MAX_PDF = 10 * 1024 * 1024  # bytes
PDF_NAMES = {"resume": "Resume.pdf", "cover_letter": "Cover-letter.pdf"}  # by slot: no version, and no person's name
ANSWER_FIRST = "Answer the decisions above first, then Open and fill appears."
CONNECT_FIRST = "Connect a browser first: Settings → Browser plugin."
AT_LINK = "SELECT id, open_manually_only FROM jobs WHERE apply_url = ?"
CONNECTED = "SELECT 1 FROM paired_browsers WHERE plugin IS NOT NULL LIMIT 1"
# A started_on later than now, from a moved clock, counts as past: never in today's count, and it causes no wait
UNUSED = """SELECT id FROM fills WHERE job_id = ? AND state = 'waiting' AND started_on BETWEEN ? AND ?
    ORDER BY id DESC LIMIT 1"""
COUNTED = "SELECT count(*) FROM fills WHERE started_on BETWEEN ? AND ?"
NEWEST = "SELECT max(started_on) FROM fills WHERE started_on <= ?"
NEW = """INSERT INTO fills (job_id, address, started_on, toolbar, browser, state)
    VALUES (?, ?, ?, ?, ?, 'waiting')"""
# started_on stays, so the pause doesn't move; a later button press never hides that a toolbar pressed, or which browser
REUSE = "UPDATE fills SET toolbar = max(toolbar, ?), browser = coalesce(?, browser) WHERE id = ?"
STARTED = "UPDATE fills SET state = 'started', browser = ?, taken_on = ? WHERE id = ?"
# A row that had its answers stays `sent` or `filled`, whatever ended it. Its read and the read's time go, unless the
# tab just left: that is how a real Submit ends, and "I submitted it" comes after
ENDED = """UPDATE fills SET state = iif(state IN ('sent', 'filled'), state, 'ended'), stop_kind = ?1,
    values_read = iif(?1 = 'gone', values_read, NULL), checked_on = iif(?1 = 'gone', checked_on, NULL) WHERE id = ?2"""
ANSWERED = "UPDATE fills SET state = ?, stop_kind = ?, changes = ? WHERE id = ?"
REPORTED = "UPDATE fills SET state = ?, stop_kind = ?, changes = ?, filled = ? WHERE id = ?"
HANDED = "UPDATE fills SET files = ? WHERE id = ?"
READ = "UPDATE fills SET values_read = ?, checked_on = ? WHERE id = ?"
CLICKED = "UPDATE fills SET clicked_on = ? WHERE id = ?"
WIPED = "UPDATE fills SET values_read = NULL, checked_on = NULL WHERE job_id = ?"
ALL_WIPED = "UPDATE fills SET values_read = NULL, checked_on = NULL"
# Only the job's newest row: a newer press that is still waiting is left alone
UNSENT = """UPDATE fills SET stop_kind = ?, values_read = NULL, checked_on = NULL WHERE state IN ('sent', 'filled')
    AND id = (SELECT max(id) FROM fills WHERE job_id = ?)"""


def form_address(job: dict) -> str | None:
    """Where the job's form is, built only from the job's record; None if the app must not open one."""
    posting = posting_of(job["apply_url"])
    if posting is None or job["open_manually_only"]:
        return None
    site, board, job_id = posting
    address = SITES[site].FORM_PAGE.format(board, job_id)
    return None if closed_site(address) or is_simplify_site(address) else address


def job_at(conn: sqlcipher3.Connection, address: str) -> int | None:
    """The job whose form is at exactly this address, as form_address writes it; anything else is no job."""
    try:
        posting = posting_of(address)
    except ValueError:  # an address that can't even be read
        return None
    if posting is None:
        return None
    site, board, job_id = posting
    link = BOARDS[site].JOB_LINK.format(board, job_id)
    row = conn.execute(AT_LINK, (link,)).fetchone()
    if row is None or form_address({"apply_url": link, "open_manually_only": row[1]}) != address:
        return None
    return row[0]


def why_not(conn: sqlcipher3.Connection, job: dict, found: preview.Preview | None, limits: Config) -> str | None:
    """None if the job can be filled now, else why not: a plain sentence, or "" where the box shows no button and says
    nothing new. `found` is the job's preview. The box, the press and /plugin/fill all ask here."""
    if form_address(job) is None or found is None or record.recorded(conn, job["id"]):
        return ""
    if found.open:
        return ANSWER_FIRST
    if conn.execute(CONNECTED).fetchone() is None:
        return CONNECT_FIRST
    moment = now()
    if _unused(conn, job["id"], moment) is not None:  # a repeat press neither counts nor waits
        return None
    cap = limits.fills_per_day
    if conn.execute(COUNTED, (_midnight(moment), moment)).fetchone()[0] >= cap:
        used = "1 fill is" if cap == 1 else f"{cap} fills are"
        return f"Today's {used} used: more tomorrow, or raise the number in Settings."
    newest = conn.execute(NEWEST, (moment,)).fetchone()[0]
    if newest is None:
        return None
    passed = datetime.fromisoformat(moment) - datetime.fromisoformat(newest)
    minutes = math.ceil((limits.fill_pause_minutes * 60 - passed.total_seconds()) / 60)
    if minutes > 0:
        left = "1 minute" if minutes == 1 else f"{minutes} minutes"
        return f"The next fill can start in {left}: that's the pause you set in Settings."
    return None


def why_not_read(conn: sqlcipher3.Connection, job: dict) -> str | None:
    """None if the plugin can read the job's form now, else why not: a plain sentence, or "" where the box shows no
    button and says nothing new. Only a form the app can't read itself is read this way."""
    address = form_address(job)
    if address is None or posting_of(address)[0] != "ashby" or record.recorded(conn, job["id"]):
        return ""
    if runs.last_done(conn, job["id"]) is None:
        return ""
    return None if conn.execute(CONNECTED).fetchone() else CONNECT_FIRST


class BadForm(Exception):  # noqa: N818 (a refusal)
    """The list a browser sent can't be read."""


@dataclass
class Ticket:
    id: str
    job: int
    address: str  # the exact address a browser must ask from
    site: str
    fill: int | None  # its row in `fills`; a read has none, so the row updates match nothing
    browser: int | None  # the paired browser; None for a button press until one takes it
    ends: float
    taken: bool = False
    answered: bool = False  # the browser has had its answer: it gets one, so a reload can't fill again
    changes: list[dict] = field(default_factory=list)  # all the compare said, uncut, so a report's count stays true
    questions: dict[str, str] = field(default_factory=dict)  # each key in the pack, with the app's own saved label
    reported: bool = False  # the browser has said how it went: it says so once
    files: dict[str, tuple[Path, str]] = field(default_factory=dict)  # each PDF the pack named, with its SHA-256 then
    handed: list[str] = field(default_factory=list)  # the slots given out: each goes once
    known: frozenset[str] = frozenset()  # the keys a read may keep as they are
    pdfs: dict[str, str] = field(default_factory=dict)  # each file key in the pack, with the name its PDF goes by
    read: dict = field(default_factory=dict)  # the read held on the row
    clicked: float | None = None  # when you last pressed the site's Submit
    mode: str = "fill"  # or "read": the plugin reads the form and fills nothing


@dataclass
class Tickets:
    """The fills that can start or are running, one per job. `end_*` mark the rows without saving, so the caller's own
    `with conn:` holds them with its change, which comes first: a busy database then drops nothing."""

    clock: Callable[[], float] = time.monotonic
    tickets: dict[int, Ticket] = field(default_factory=dict)  # by job

    def make(
        self,
        conn: sqlcipher3.Connection,
        job: dict,
        found: preview.Preview | None,
        limits: Config,
        browser_id: int | None = None,
        name: str | None = None,
    ) -> str | None:
        """The rules, then the row, then the ticket, with nothing to wait for in between, so two presses can't both
        slip under the cap; `browser_id` and `name` are the toolbar's. Why not (see why_not) if refused, else None. A
        busy database raises, and leaves no ticket."""
        reason = why_not(conn, job, found, limits)
        if reason is not None:
            return reason
        old, moment, address = self._live().get(job["id"]), now(), form_address(job)
        toolbar, unused = browser_id is not None, _unused(conn, job["id"], moment)
        with conn:
            conn.execute(WIPED, (job["id"],))
            if old and old.taken:
                conn.execute(ENDED, (REPLACED, old.fill))
            if unused is None:
                fill = conn.execute(NEW, (job["id"], address, moment, toolbar, name)).lastrowid
            else:
                fill = unused
                conn.execute(REUSE, (toolbar, name, fill))
        self.tickets[job["id"]] = Ticket(
            secrets.token_urlsafe(32), job["id"], address, posting_of(job["apply_url"])[0], fill, browser_id,
            self.clock() + UNUSED_SECONDS,
        )  # fmt: skip
        return None

    def make_read(self, conn: sqlcipher3.Connection, job: dict) -> str | None:
        """A ticket to read the job's form. It has no row, so no cap, pause or trail, but it is a newer press all the
        same: it ends a running fill and wipes the job's reads. Why not (see why_not_read) if refused, else None."""
        reason = why_not_read(conn, job)
        if reason is not None:
            return reason
        old, address = self._live().get(job["id"]), form_address(job)
        with conn:
            conn.execute(WIPED, (job["id"],))
            if old and old.taken:
                conn.execute(ENDED, (REPLACED, old.fill))
        self.tickets[job["id"]] = Ticket(
            secrets.token_urlsafe(32), job["id"], address, posting_of(address)[0], None, None,
            self.clock() + UNUSED_SECONDS, mode="read",
        )  # fmt: skip
        return None

    def take(self, conn: sqlcipher3.Connection, address: str, browser_id: int, name: str) -> Ticket | None:
        """The untaken ticket for exactly this address, once, now this browser's; a toolbar ticket only for its own
        browser. The row is saved first, so a busy database raises and uses nothing up."""
        for ticket in self._live().values():
            if ticket.address == address and not ticket.taken and ticket.browser in (None, browser_id):
                with conn:
                    conn.execute(STARTED, (name, now(), ticket.fill))
                ticket.browser, ticket.taken, ticket.ends = browser_id, True, self.clock() + RUNNING_SECONDS
                return ticket
        return None

    def running(self, ticket_id: str, browser_id: int) -> Ticket | None:
        """This browser's taken ticket with this id."""
        for ticket in self._live().values():
            if ticket.id == ticket_id and ticket.taken and ticket.browser == browser_id:
                return ticket
        return None

    def answer(self, conn: sqlcipher3.Connection, data_dir: Path, ticket: Ticket, items: list) -> list[dict] | None:
        """What goes to the browser, from the fields it says the form shows; None for a stop, and an empty list when
        the form matched with nothing to fill. Every ending is saved here. BadForm if the list can't be read."""
        if len(items) > MAX_FIELDS:  # none of them is read
            self.save_answer(conn, ticket, [], TOO_MANY_FIELDS)
            return None
        try:
            fields = compare.fields_from(items)
        except ValueError:
            raise BadForm from None
        found = preview.preview(conn, data_dir, ticket.job)
        ticket.known = frozenset(
            line.field.key
            for line in (found.lines if found else ())
            if line.status != "hidden" and line.field.kind != "file" and "password" not in line.field.label.lower()
        )
        passed, changes = compare.compare(found.lines if found else (), fields, SITES[ticket.site].DRAWN)
        if not passed:
            self.save_answer(conn, ticket, changes, UNEXPECTED_PAGE)
            return None
        documents = record.documents(conn, data_dir, ticket.job)
        answers = compare.pack(passed, documents)
        # the trail first: if this can't be saved, nothing goes out
        self.save_answer(conn, ticket, changes, None if answers else NOTHING_TO_FILL)
        labels = {line.field.key: line.field.label for line, _ in passed}
        ticket.questions = {answer["key"]: labels[answer["key"]] for answer in answers}
        slots = {answer["key"]: answer["value"]["slot"] for answer in answers if answer["kind"] == "file"}
        ticket.files = {slot: (documents[slot][1], record.sha256(documents[slot][1])) for slot in slots.values()}
        ticket.pdfs = {key: PDF_NAMES[slot] for key, slot in slots.items()}
        return answers

    def read_form(self, conn: sqlcipher3.Connection, ticket: Ticket, items: list) -> bool:
        """Saves the fields a browser says the form shows as the job's form, and says if it did; either way that uses
        the ticket up, and BadForm if the list can't be read leaves it. Saved first: a busy database raises and uses
        nothing up. Saved answers stay, by their keys."""
        fields = ()
        if len(items) <= MAX_FIELDS:  # none of them is read otherwise
            try:
                fields = SITES[ticket.site].form(compare.fields_from(items)).fields
            except ValueError:
                raise BadForm from None
        if fields:
            save(conn, ticket.job, Form(fields), "browser")
        del self.tickets[ticket.job]
        return bool(fields)

    def closed_by_record(self, conn: sqlcipher3.Connection, ticket: Ticket) -> bool:
        """Whether the job has its record, which ends the ticket: a busy database can save "I submitted it" and not end
        its fill."""
        if not record.recorded(conn, ticket.job):
            return False
        with conn:
            self.end_job(conn, ticket.job)
        return True

    def save_answer(
        self, conn: sqlcipher3.Connection, ticket: Ticket, changes: list[dict], stop: str | None = None
    ) -> None:
        """Saves how the browser's answer ended, before it goes out: a busy database raises and uses nothing up. `stop`
        is why nothing goes out; that ends the ticket, so a later press can't overwrite why."""
        state = "sent" if stop is None else "stopped"
        with conn:
            conn.execute(ANSWERED, (state, stop, _json(compare.at_most(changes)), ticket.fill))
        ticket.answered, ticket.changes = True, changes
        if stop:
            del self.tickets[ticket.job]

    def report(
        self, conn: sqlcipher3.Connection, ticket: Ticket, stop: str | None, filled: list[str], left: dict[str, str]
    ) -> None:
        """How the fill went, from the browser: `filled` keys, and each `left` key with why, which wins, as a line that
        didn't stick is in both. Only the pack's keys count, each said in the app's own words, so nothing a page wrote
        is kept. What you must act on comes first, so it's never lost in the count. Saved first: a busy database raises
        and changes nothing. `stop` is why it stopped; that ends the ticket, and a row with its answers stays `sent`."""
        state = "filled" if stop is None else "sent" if ticket.answered else "stopped"
        questions = ticket.questions.items()
        lines = [{"question": question, "what": LEFT[left[key]]} for key, question in questions if key in left]
        changes = compare.at_most(lines + ticket.changes)
        done = [question for key, question in questions if key in filled and key not in left]
        with conn:
            conn.execute(REPORTED, (state, stop, _json(changes), _json(done), ticket.fill))
        ticket.reported = True
        if stop:
            del self.tickets[ticket.job]

    def hand(self, conn: sqlcipher3.Connection, ticket: Ticket, slot: str) -> None:
        """Saves that this PDF goes out, before it does: a busy database raises and uses nothing up."""
        handed = [*ticket.handed, slot]
        with conn:
            conn.execute(HANDED, (_json(handed), ticket.fill))
        ticket.handed = handed

    def reread(self, conn: sqlcipher3.Connection, ticket: Ticket, values: dict, clicked: bool) -> None:
        """What the form holds now, kept only for keys the app knows; of a file box only whether it still shows our PDF,
        as another file's name may hold a person's. It replaces the held read whole, or not at all: it must have
        something filled and at least half as many as the held one, so a form reset after Submit is ignored. `clicked`
        is your press of Submit, which counts even when the read doesn't. Saved first: a busy database raises and
        changes nothing."""
        read = {key: _clean(value) for key, value in values.items() if key in ticket.known}
        read |= {key: _file_verdict(values[key], name) for key, name in ticket.pdfs.items() if key in values}
        count, held = sum(map(_filled, read.values())), sum(map(_filled, ticket.read.values()))
        accepted, moment = count >= 1 and 2 * count >= held, now()
        with conn:
            if accepted:
                conn.execute(READ, (_json(read), moment, ticket.fill))
            if clicked:
                conn.execute(CLICKED, (moment, ticket.fill))
        if accepted:
            ticket.read = read
        if clicked:
            ticket.clicked = self.clock()

    def can_thank(self, ticket: Ticket) -> bool:
        """Whether a thank-you page counts for this ticket: its site is switched on, and you pressed Submit just now."""
        pressed = ticket.clicked is not None and self.clock() - ticket.clicked <= THANKS_SECONDS
        return pressed and SITES[ticket.site].DRAWN.thanks is not None

    def thanked(self, conn: sqlcipher3.Connection, data_dir: Path, ticket: Ticket, job: dict) -> bool:
        """The site's thank-you page after your press: records it as "I submitted it" would, with today's date, and says
        if it did. A refused record changes nothing, so the box still says it looks sent."""
        found = preview.preview(conn, data_dir, ticket.job)
        try:
            record.submit(conn, data_dir, job, date.today(), found.lines if found else None)
        except record.Refused:
            return False
        with conn:
            self._end(conn, [(ticket, THANKED)])
        return True

    def gone(self, conn: sqlcipher3.Connection, ticket: Ticket) -> None:
        """The form's tab closed or moved on: the fill ends, its read stays and nothing is recorded."""
        with conn:
            self._end(conn, [(ticket, GONE)])

    def not_sent(self, conn: sqlcipher3.Connection, job_id: int) -> None:
        """I didn't send it: closes the job's newest fill if its answers went, with or without a ticket. Nothing is
        recorded, and the fill still counts: it went out."""
        with conn:
            closed = conn.execute(UNSENT, (NOT_SENT, job_id)).rowcount
        if closed:
            self.tickets.pop(job_id, None)

    def end_browser(self, conn: sqlcipher3.Connection, browser_id: int) -> None:
        """A removed browser's running and toolbar tickets; a button ticket nobody took stays for another browser. Every
        read goes, as a row names its browser only in words."""
        self._wipe_all(conn)
        self._end(conn, [(ticket, BROWSER_REMOVED) for ticket in self._live().values() if ticket.browser == browser_id])

    def end_all(self, conn: sqlcipher3.Connection) -> None:
        """Every ticket and read, for "Log out all devices"; a button ticket nobody took had no browser to remove."""
        self._wipe_all(conn)
        live = self._live().values()
        self._end(conn, [(ticket, LOGGED_OUT if ticket.browser is None else BROWSER_REMOVED) for ticket in live])

    def end_job(self, conn: sqlcipher3.Connection, job_id: int) -> None:
        """The job's ticket, if it has one, at "I submitted it"."""
        ticket = self._live().get(job_id)
        self._end(conn, [(ticket, SUBMITTED)] if ticket else [])

    def _end(self, conn: sqlcipher3.Connection, ended: list[tuple[Ticket, str]]) -> None:
        conn.executemany(ENDED, [(kind, ticket.fill) for ticket, kind in ended])
        for ticket, _ in ended:
            del self.tickets[ticket.job]

    def _wipe_all(self, conn: sqlcipher3.Connection) -> None:
        conn.execute(ALL_WIPED)
        for ticket in self.tickets.values():
            ticket.read = {}

    def _live(self) -> dict[int, Ticket]:
        """The tickets still in time; the others are forgotten, and their rows stay as they are."""
        moment = self.clock()
        self.tickets = {job: ticket for job, ticket in self.tickets.items() if moment <= ticket.ends}
        return self.tickets


def named_pdf(data_dir: Path, ticket: Ticket, slot: str) -> bytes | None:
    """The PDF the pack named, byte for byte, so a newer one can't slip in; None if it isn't a plain file in the job's
    own folder, is too big, isn't a PDF or has changed. A link can't lead out, as only real paths are compared."""
    path, sha256 = ticket.files[slot]
    folder = data_dir.resolve() / "documents" / str(ticket.job)
    try:
        real = path.resolve()
        found = real.stat()
        if not (real.is_relative_to(folder) and stat.S_ISREG(found.st_mode) and found.st_size <= MAX_PDF):
            return None
        data = real.read_bytes()
    except (OSError, RuntimeError):  # RuntimeError: a link that leads to itself
        return None
    return data if data.startswith(b"%PDF-") and hashlib.sha256(data).hexdigest() == sha256 else None


def _json(items: list | dict) -> str:
    return json.dumps(items, ensure_ascii=False)


def _clean(value: str | list[str]) -> str | list[str]:
    return CONTROL.sub("", value) if isinstance(value, str) else [CONTROL.sub("", text) for text in value]


def _file_verdict(value: str | list[str], name: str) -> str:
    shown = _clean(value)
    if not _filled(shown):
        return ""
    return SAME_FILE if shown == name else CHANGED_FILE


def _filled(value: str | list[str]) -> bool:
    return any(text.strip() for text in ([value] if isinstance(value, str) else value))


def _unused(conn: sqlcipher3.Connection, job_id: int, moment: str) -> int | None:
    """The row of the job's fill made today that no browser took, even if its ticket has gone: nothing went out."""
    row = conn.execute(UNUSED, (job_id, _midnight(moment), moment)).fetchone()
    return row[0] if row else None


def _midnight(moment: str) -> str:
    """When the laptop's own day began, in UTC as `started_on` is, so an evening in Phoenix is still today."""
    day = datetime.fromisoformat(moment).astimezone().date()
    return datetime.combine(day, datetime.min.time()).astimezone(UTC).isoformat(timespec="seconds")
