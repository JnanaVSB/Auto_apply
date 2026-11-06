"""A fill (Phase 7): one press of Open and fill or the plugin's toolbar. Here are the form's address, the rules for a
fill, the `fills` row that counts it (what the daily cap and the pause read) and its ticket, the one-time pass a browser
needs to start. Tickets live in memory: stopping the dashboard ends them all, and the rows stay."""

import math
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import sqlcipher3

from app.collect import now
from app.config import Config
from app.sources import closed_site, is_simplify_site, posting_of
from app.sources.boards import BOARDS
from app.submit import ashby, greenhouse, lever, record
from app.submit.preview import Preview

SITES = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}
UNUSED_SECONDS = 5 * 60  # a ticket no browser has taken
RUNNING_SECONDS = 2 * 60 * 60  # a taken ticket
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
STARTED = "UPDATE fills SET state = 'started', browser = ? WHERE id = ?"
ENDED = "UPDATE fills SET state = 'ended', stop_kind = ? WHERE id = ?"


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


def why_not(conn: sqlcipher3.Connection, job: dict, found: Preview | None, limits: Config) -> str | None:
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


@dataclass
class Ticket:
    id: str
    job: int
    address: str  # the exact address a browser must ask from
    site: str
    fill: int  # its row in `fills`
    browser: int | None  # the paired browser; None for a button press until one takes it
    ends: float
    taken: bool = False


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
        found: Preview | None,
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
            if old and old.taken:
                conn.execute(ENDED, ("replaced", old.fill))
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

    def take(self, conn: sqlcipher3.Connection, address: str, browser_id: int, name: str) -> Ticket | None:
        """The untaken ticket for exactly this address, once, now this browser's; a toolbar ticket only for its own
        browser. The row is saved first, so a busy database raises and uses nothing up."""
        for ticket in self._live().values():
            if ticket.address == address and not ticket.taken and ticket.browser in (None, browser_id):
                with conn:
                    conn.execute(STARTED, (name, ticket.fill))
                ticket.browser, ticket.taken, ticket.ends = browser_id, True, self.clock() + RUNNING_SECONDS
                return ticket
        return None

    def running(self, ticket_id: str, browser_id: int) -> Ticket | None:
        """This browser's taken ticket with this id."""
        for ticket in self._live().values():
            if ticket.id == ticket_id and ticket.taken and ticket.browser == browser_id:
                return ticket
        return None

    def end_browser(self, conn: sqlcipher3.Connection, browser_id: int) -> None:
        """A removed browser's running and toolbar tickets; a button ticket nobody took stays for another browser."""
        self._end(conn, [ticket for ticket in self._live().values() if ticket.browser == browser_id], "browser-removed")

    def end_all(self, conn: sqlcipher3.Connection) -> None:
        """Every ticket, for "Log out all devices"."""
        self._end(conn, list(self._live().values()), "browser-removed")

    def end_job(self, conn: sqlcipher3.Connection, job_id: int) -> None:
        """The job's ticket, if it has one, at "I submitted it"."""
        ticket = self._live().get(job_id)
        self._end(conn, [ticket] if ticket else [], "submitted")

    def _end(self, conn: sqlcipher3.Connection, ended: list[Ticket], kind: str) -> None:
        conn.executemany(ENDED, [(kind, ticket.fill) for ticket in ended])
        for ticket in ended:
            del self.tickets[ticket.job]

    def _live(self) -> dict[int, Ticket]:
        """The tickets still in time; the others are forgotten, and their rows stay as they are."""
        moment = self.clock()
        self.tickets = {job: ticket for job, ticket in self.tickets.items() if moment <= ticket.ends}
        return self.tickets


def _unused(conn: sqlcipher3.Connection, job_id: int, moment: str) -> int | None:
    """The row of the job's fill made today that no browser took, even if its ticket has gone: nothing went out."""
    row = conn.execute(UNUSED, (job_id, _midnight(moment), moment)).fetchone()
    return row[0] if row else None


def _midnight(moment: str) -> str:
    """When the laptop's own day began, in UTC as `started_on` is, so an evening in Phoenix is still today."""
    day = datetime.fromisoformat(moment).astimezone().date()
    return datetime.combine(day, datetime.min.time()).astimezone(UTC).isoformat(timespec="seconds")
