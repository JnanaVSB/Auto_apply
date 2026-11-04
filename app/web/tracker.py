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

from app import collect, db, tracker
from app.config import Config, ConfigError, load_config
from app.fetch import Fetcher, FetchError
from app.keychain import KeychainError
from app.sources import SourceError
from app.web import actions, queries

PAGE = "/tracker"
STALE = "Already moved in another tab: the board shows where it is now."
NOTICES = {"stale": STALE, "round": "Round saved."}  # ?notice=…; ?moved=<stage> says "Moved to …"


def add_routes(app: FastAPI, page: Callable[..., HTMLResponse], data_dir: Path) -> None:
    def board(request: Request, status: int = 200, **values) -> HTMLResponse:
        try:
            stale_days = load_config().tracker_stale_days
        except ConfigError:  # shown on the Settings page; the default meanwhile
            stale_days = Config.model_fields["tracker_stale_days"].default
        show_all = request.query_params.get("all")
        lanes = tracker.board(request.app.state.conn, datetime.now().astimezone(), stale_days,
                              show_all if show_all in tracker.STAGES else None)  # fmt: skip
        return page(request, "tracker.html", status, lanes=lanes, stale_days=stale_days, today=date.today(),
                    stages=tracker.STAGES, rounds=tracker.ROUNDS, **values)  # fmt: skip

    @app.get(PAGE)
    async def tracker_page(request: Request) -> Response:
        moved, notice = request.query_params.get("moved"), request.query_params.get("notice")
        return board(request, notice=f"Moved to {tracker.STAGES[moved]}." if moved in tracker.STAGES
                     else NOTICES.get(notice))  # fmt: skip

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
        return RedirectResponse(f"{PAGE}?{f'moved={to}' if moved else 'notice=stale'}", 303)

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
        except (ValueError, collect.CollectError, ConfigError, KeychainError, db.DatabaseError) as error:
            return board(request, 400, problem=str(error), typed=form)
        except (FetchError, SourceError) as error:
            return board(request, 400, problem=f"The job's board could not be read ({error}).", typed=form)
        return board(request, notice=notice)


def job_box(conn: sqlcipher3.Connection, job_id: int) -> dict | None:
    """The job page's Tracker box: where the card is, whether the board shows it (not a Not interested job in Ready)
    and every move, newest first, at this computer's local time; None for a job never on the board."""
    moves = tracker.history(conn, job_id)
    if not moves:
        return None
    lane, round_, skipped = conn.execute(
        "SELECT coalesce(t.stage, 'ready'), t.round, j.not_interested FROM jobs j "
        "LEFT JOIN tracker t ON t.job_id = j.id WHERE j.id = ?",
        (job_id,),
    ).fetchone()
    return {
        "lane": lane,
        "stage": tracker.STAGES[lane],
        "shown": not (lane == "ready" and skipped),
        "round": tracker.ROUNDS.get(round_),
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
