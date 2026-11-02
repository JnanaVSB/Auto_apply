"""The Pilot page (/pilot), kept apart from main.py: start a pilot on 20 ready jobs, run each line-up on it, compare
the line-ups, mark each objection useful or not, and copy a result without a made-up claim into its job."""

import json
from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException

from app.config import load_config
from app.council import pilot, runs
from app.resume import ResumeError, read_resume
from app.scoring import ready
from app.web import council_view, queries
from app.web.council_settings import MEMBERS, NAMES, PRESETS

PAGE = "/pilot"
OFFERED = 60  # ready jobs offered to choose from
LATEST = "SELECT id, jobs, planted, rounds, turns, created_on FROM pilots ORDER BY id DESC LIMIT 1"


def add_routes(app: FastAPI, page: Callable[..., HTMLResponse]) -> None:
    @app.get(PAGE)
    async def pilot_page(request: Request) -> Response:
        conn, config = request.app.state.conn, load_config()
        found = conn.execute(LATEST).fetchone()
        if found is None:
            resume = _resume(config)
            offered = ready.newest(conn, config.ai.evaluator.model, config.scoring.ready_threshold, resume, OFFERED)
            return page(request, "pilot.html", pilot=None, jobs=[queries.job(conn, job) for job in offered],
                        chosen=set(pilot.suggest(conn, config, resume)))  # fmt: skip
        number, jobs, planted, rounds, turns, created_on = found
        return page(
            request, "pilot.html", pilot={"id": number, "rounds": rounds, "turns": turns, "created_on": created_on},
            jobs=[queries.job(conn, job) for job in json.loads(jobs)], planted=json.loads(planted), names=NAMES,
            summaries={lineup: pilot.summary(conn, number, lineup) for lineup in NAMES},
        )  # fmt: skip

    @app.post(PAGE)
    async def start_pilot(request: Request) -> Response:
        conn = request.app.state.conn
        jobs = [int(name[4:]) for name in request.state.form if name.startswith("job_") and name[4:].isdigit()]
        if not 1 <= len(jobs) <= 2 * pilot.SIZE or any(queries.job(conn, queries.known(job)) is None for job in jobs):
            raise HTTPException(400, f"Choose 1 to {2 * pilot.SIZE} jobs.")
        pilot.create(conn, load_config(), jobs)
        return RedirectResponse(PAGE, 303)

    @app.post(PAGE + "/{number:int}/run/{lineup}")
    async def run_lineup(request: Request, number: int, lineup: str) -> Response:
        """The line-up on every job of the pilot, through the council like any press (its own models)."""
        if lineup not in PRESETS:
            raise HTTPException(404)
        roles = dict(zip(MEMBERS, PRESETS[lineup], strict=True))
        pilot.run(request.app.state.conn, load_config(), number, lineup, roles)
        request.app.state.preparer.wake()
        return RedirectResponse(PAGE, 303)

    @app.get(PAGE + "/{number:int}/jobs/{job_id:int}")
    async def pilot_job(request: Request, number: int, job_id: int) -> Response:
        """Each line-up's result for one job, side by side, with every objection to mark."""
        conn = request.app.state.conn
        job = queries.job(conn, queries.known(job_id))
        if job is None:
            raise HTTPException(404)
        found = {lineup: pilot.results(conn, number, lineup, job_id) for lineup in NAMES}
        return page(request, "pilot_job.html", job=job, number=number, names=NAMES, found=found,
                    labels=_labels(conn, found))  # fmt: skip

    @app.post(PAGE + "/marks")
    async def mark_objection(request: Request) -> Response:
        form = request.state.form
        step, objection = form.get("step", ""), form.get("objection", "")
        if not step.isdigit() or not objection or len(objection) > 10:
            raise HTTPException(400, "Mark one objection.")
        pilot.mark(request.app.state.conn, int(step), objection, form.get("useful") == "1")
        return RedirectResponse(form.get("back", PAGE) if form.get("back", "").startswith(PAGE) else PAGE, 303)

    @app.post(PAGE + "/use/{preparation:int}")
    async def use_result(request: Request, preparation: int) -> Response:
        """A result without a made-up claim becomes the job's newest version, with its PDF."""
        conn = request.app.state.conn
        copied = pilot.use(conn, preparation)
        if copied is None:
            raise HTTPException(404)
        await council_view.rebuild(conn, copied)
        job_id = conn.execute("SELECT job_id FROM preparations WHERE id = ?", (copied,)).fetchone()[0]
        return RedirectResponse(f"/jobs/{job_id}#preparation", 303)


def _resume(config) -> str | None:
    try:
        return read_resume(config.data_dir).fingerprint
    except ResumeError:
        return None


def _labels(conn, found: dict) -> dict:
    """Each entry's name by (source, entry id), for showing the drafts side by side."""
    names = {}
    for result in found.values():
        if result and result["draft"]:
            preparation = result["id"]
            recorded = conn.execute("SELECT sources FROM preparations WHERE id = ?", (preparation,)).fetchone()[0]
            names |= {(e.source, e.tile.tile_id): e.tile.facts.get("name") or e.label
                      for e in runs.originals(conn, recorded)}  # fmt: skip
    return names
