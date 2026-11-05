"""Settings → Export (/settings/export), kept apart from main.py: the warning, the folder, Export now, the schedule and
what the last run said. The files aren't encrypted, so nothing is exported unasked: every press needs the tick, checked
here, and the typed path never goes into an address or a log."""

import re
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app.config import ConfigError, ExportSettings, load_config
from app.export import folder, papers
from app.web import actions
from app.web.exporter import Exporter

PAGE = "/settings/export"
ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
TICK = "Tick “I understand these files aren't encrypted” first."
BAD_DATE = "Give each date as year-month-day, or leave it empty."
DATE_ORDER = "The From date comes first."
MOST_DAYS = 365
BAD_DAYS = f"Give the days as a whole number from 0 to {MOST_DAYS}."
DONE = {  # ?done=…: fixed words only
    "folder": "Folder saved.",
    "changed": "Folder saved. Earlier exports stay where they are.",
    "schedule": "Schedule saved.",
}
RUNS = "SELECT tried_on, last_result FROM export_runs WHERE id = 1"


def add_routes(app: FastAPI, page: Callable[..., HTMLResponse], data_dir: Path) -> None:
    app.state.exporter = Exporter(data_dir)

    @app.get(PAGE)
    async def export_page(request: Request) -> Response:
        return _view(request, page, notice=DONE.get(request.query_params.get("done", "")))

    @app.post(PAGE + "/folder")
    async def save_folder(request: Request) -> Response:
        typed = request.state.form.get("folder", "").strip()
        try:
            path = str(await run_in_threadpool(folder.make, typed, data_dir))  # a network disk can be slow
        except folder.Refused as refusal:
            return _view(request, page, 400, problem=str(refusal), typed_folder=typed)
        before = _settings()[0].folder
        if problem := _save({"folder": path}):
            return _view(request, page, 400, problem=problem, typed_folder=typed)
        return RedirectResponse(f"{PAGE}?done={'changed' if before and before != path else 'folder'}", 303)

    @app.post(PAGE + "/run")
    async def export_now(request: Request) -> Response:
        form = request.state.form
        settings, problem = _settings()
        typed = {"typed_start": form.get("start", ""), "typed_end": form.get("end", "")}
        if problem is None and form.get("tick") != "on":
            problem = TICK
        if problem is None and not settings.folder:
            problem = folder.CHOOSE
        if problem is None:
            try:
                start, end = _date(form.get("start", "")), _date(form.get("end", ""))
                problem = DATE_ORDER if start and end and end < start else None
            except ValueError:
                problem = BAD_DATE
        problem = problem or request.app.state.exporter.start(start, end)
        if problem:
            return _view(request, page, 400, problem=problem, **typed)
        return RedirectResponse(PAGE, 303)

    @app.post(PAGE + "/schedule")
    async def save_schedule(request: Request) -> Response:
        form = request.state.form
        typed = form.get("every_days", "")
        days = actions.whole(typed)
        if not isinstance(days, int) or days > MOST_DAYS:
            return _view(request, page, 400, problem=BAD_DAYS, typed_days=typed)
        if days > 0 and form.get("tick") != "on":
            return _view(request, page, 400, problem=TICK, typed_days=typed)
        if problem := _save({"every_days": days}):
            return _view(request, page, 400, problem=problem, typed_days=typed)
        return RedirectResponse(f"{PAGE}?done=schedule", 303)

    @app.get(PAGE + "/status")
    async def status(request: Request) -> Response:
        return page(request, "export_status.html", **_status(request))


def _settings() -> tuple[ExportSettings, str | None]:
    """The saved settings, or the defaults and why they can't be read."""
    try:
        return load_config().export, None
    except ConfigError as error:
        return ExportSettings(), str(error)


def _save(change: dict) -> str | None:
    return actions.update_settings(lambda old: old | {"export": dict(old.get("export") or {}) | change})


def _date(text: str) -> date | None:
    """A typed date, or None for an empty box; ValueError for anything else (fromisoformat alone takes 20261105)."""
    text = text.strip()
    if not text:
        return None
    if not ISO_DATE.fullmatch(text) or not text.isascii():
        raise ValueError
    return date.fromisoformat(text)


def _status(request: Request) -> dict:
    """What the status part shows: the run in progress, or the last result, this dashboard's words first."""
    exporter = request.app.state.exporter
    row = request.app.state.conn.execute(RUNS).fetchone()
    return {"exporter": exporter, "result": exporter.notice or (row and row[1])}


def _view(request: Request, page: Callable[..., HTMLResponse], status: int = 200, **values) -> HTMLResponse:
    settings, problem = _settings()
    values["problem"] = values.get("problem") or problem
    row = request.app.state.conn.execute(RUNS).fetchone()
    last_run = papers.day(row[0]) if row and row[0] else None
    if last_run and settings.every_days:
        values["last_run"] = last_run
        values["next_run"] = (date.fromisoformat(last_run) + timedelta(days=settings.every_days)).isoformat()
    return page(request, "export_settings.html", status, settings=settings, **_status(request), **values)
