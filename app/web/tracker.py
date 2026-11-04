"""The Tracker page (/tracker), kept apart from main.py: the board, I applied, Move to, the interview round and Add an
application. Every change is a form (no JavaScript needed); a notice comes back as a fixed word in the address."""

from collections.abc import Callable
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

import sqlcipher3
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from app import db, tracker
from app.config import Config, ConfigError, load_config
from app.fetch import Fetcher
from app.gmail import apply
from app.web import actions, queries

PAGE = "/tracker"
# ?notice=<code> shows one of these fixed lines; anything else in the address shows nothing
NOTICES = {stage: f"Moved to {name}." for stage, name in tracker.STAGES.items()} | {
    "stale": tracker.STALE,
    "round": "Round saved.",
}


def add_routes(app: FastAPI, page: Callable[..., HTMLResponse], data_dir: Path) -> None:
    def board(request: Request, status: int = 200, **values) -> HTMLResponse:
        try:
            stale_days = load_config().tracker_stale_days
        except ConfigError:  # shown on the Settings page; the default meanwhile
            stale_days = Config.model_fields["tracker_stale_days"].default
        local, conn = datetime.now().astimezone(), request.app.state.conn
        lanes = tracker.board(conn, local, stale_days, request.query_params.get("all"))
        try:  # "Add as an application" from E-mails to check: the e-mail's day
            values.setdefault("add_on", date.fromisoformat(request.query_params.get("applied", "")))
        except ValueError:
            pass
        return page(request, "tracker.html", status, lanes=lanes, stale_days=stale_days, today=local.date(),
                    stages=tracker.STAGES, rounds=tracker.ROUNDS, to_check=apply.waiting(conn), **values)  # fmt: skip

    @app.get(PAGE)
    async def tracker_page(request: Request) -> Response:
        return board(request, notice=NOTICES.get(request.query_params.get("notice")))

    @app.post(PAGE + "/{job_id:int}/move")
    async def move_card(request: Request, job_id: int) -> Response:
        conn, form = request.app.state.conn, request.state.form
        _check_job(conn, job_id)
        start, to = form.get("from"), form.get("to")
        if start not in tracker.STAGES or to not in tracker.STAGES or start == to:
            raise HTTPException(400, "Choose a stage from the list.")
        if start == "ready" and not tracker.on_board(conn, job_id):
            raise HTTPException(400, "This job isn't on the Tracker yet: it has no finished resume version.")
        moved = tracker.move(conn, job_id, start, to, date.today())
        return RedirectResponse(f"{PAGE}?notice={to if moved else 'stale'}", 303)

    @app.post(PAGE + "/{job_id:int}/round")
    async def save_round(request: Request, job_id: int) -> Response:
        conn, round_ = request.app.state.conn, request.state.form.get("round")
        _check_job(conn, job_id)
        if round_ not in tracker.ROUNDS:
            raise HTTPException(400, "Choose a round from the list.")
        if tracker.set_round(conn, job_id, round_):
            return RedirectResponse(f"{PAGE}?notice=round", 303)
        if tracker.stage(conn, job_id) != "interview":  # moved meanwhile
            return RedirectResponse(f"{PAGE}?notice=stale", 303)
        return RedirectResponse(PAGE, 303)  # the same round: nothing to say

    @app.post(PAGE + "/add")
    async def add_application(request: Request) -> Response:
        """Answered with the board itself, not a redirect: pressing Add again only says it is already there."""
        form = request.state.form
        try:
            notice, _ = await run_in_threadpool(_add, data_dir, request.app.state.collector.fetcher, form)
        except (ValueError, *actions.CANT_SAVE) as error:
            return board(request, 400, problem=str(error), typed=form)
        except actions.CANT_READ as error:
            return board(request, 400, problem=f"The job's board could not be read ({error}).", typed=form)
        return board(request, notice=notice)


def job_box(conn: sqlcipher3.Connection, job_id: int) -> dict | None:
    """The job page's Tracker box: where the card is, whether the board shows it, and every move and e-mail, newest
    first, at this computer's local time; None for a job with neither (a Ready card no one has moved and no e-mail
    has named yet)."""
    moves, emails = tracker.history(conn, job_id), apply.of_job(conn, job_id)
    if not moves and not emails:
        return None
    lane, round_, shown = tracker.place(conn, job_id)
    return {
        "lane": lane,
        "stage": tracker.STAGES[lane],
        "shown": shown,
        "round": tracker.ROUNDS.get(round_),
        "emails": emails,
        "moves": [
            {
                "when": datetime.fromisoformat(move["moved_on"]).astimezone().strftime("%Y-%m-%d %H:%M"),
                "from": tracker.STAGES[move["from_stage"]],
                "to": tracker.STAGES[move["to_stage"]],
                "round": tracker.ROUNDS.get(move["round"]),
                "by": "Gmail" if move["moved_by"] == "gmail" else "you",
            }
            for move in moves
        ],
    }


def _check_job(conn: sqlcipher3.Connection, job_id: int) -> None:
    """A job number that isn't saved is "Page not found"."""
    if conn.execute("SELECT 1 FROM jobs WHERE id = ?", (queries.known(job_id),)).fetchone() is None:
        raise HTTPException(404)


def _add(data_dir: Path, fetcher: Fetcher, form: dict[str, str]) -> tuple[str, int]:
    """Add an application with its own connection, one add at a time (like Add a job)."""
    try:
        applied_on = date.fromisoformat(form.get("applied_on", ""))
    except ValueError:
        raise ValueError(tracker.BAD_DATE) from None
    with actions.ADDING, closing(db.connect(data_dir)) as conn:
        return tracker.add_application(conn, fetcher, form.get("link", ""), form.get("company", ""),
                                       form.get("role", ""), applied_on, date.today())  # fmt: skip
