"""The council's buttons on a job's page, kept apart from main.py and prepare.py: the wording switch after no
agreement, "Now, normal price", "Continue anyway" and Request changes for cover letters (each answers with the job's
page), and the top bar on every page. What the box shows of the council is in app/web/drafts.py."""

import json
from collections.abc import Callable

import sqlcipher3
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from app.config import REVIEWERS, ConfigError, load_config
from app.council import estimate, flow, reviewers, runs, spending
from app.documents import pdf, privacy
from app.resume import CONTROL, ResumeError
from app.web import drafts, prepare, queries
from app.web.council_settings import tier

CONTINUE = """UPDATE preparations SET status = 'writing', waived = ?, problem = NULL, finished_on = NULL
    WHERE id = ? AND status = 'stopped'"""


def topbar(conn: sqlcipher3.Connection) -> dict | None:
    """The top bar: the line-up with its estimate per job, today's and this month's spend against your limits, a
    pause notice, and per member the model, company and estimate at 3 and 5 rounds."""
    try:
        config = load_config()
    except ConfigError:
        return None
    turns, found = config.council.turns, spending.spent(conn, spending.today())
    members = [("Writer", "writer", config.ai.writer)] + [
        (reviewers.MEMBERS[member][0], member, getattr(config.ai, member)) for member in REVIEWERS
    ] + [(added.name, added.id, added.role) for added in config.ai.extra]  # fmt: skip
    rows = [{"name": name, "role": role, "at3": estimate.member(conn, member, role, 3, turns, half=True),
             "at5": estimate.member(conn, member, role, 5, turns, half=True)}
            for name, member, role in members if role]  # fmt: skip
    per_job = _total([row["at3"] for row in rows]) if len(rows) == len(members) else None
    paused = (
        "Paused: today's limit reached" if found.today >= config.spending.daily
        else "Paused: this month's limit reached" if found.month >= config.spending.monthly else None
    )  # fmt: skip
    return {
        "tier": tier(config), "per_job": per_job, "today": found.today, "month": found.month, "paused": paused,
        "daily": config.spending.daily, "monthly": config.spending.monthly, "rows": rows,
        "per_job5": _total([row["at5"] for row in rows]) if per_job is not None else None,
        "per_month": per_job * config.council.daily_jobs * 30 if per_job is not None else None,
    }  # fmt: skip


def add_routes(app: FastAPI, page: Callable[..., HTMLResponse], scoring: Callable[[], queries.Scoring]) -> None:
    @app.post("/jobs/{job_id:int}/wording")
    async def switch_wording(request: Request, job_id: int) -> Response:
        """No agreement: the PDF in your original wording where the Truth checker objected, or in the Writer's."""
        conn, wording = request.app.state.conn, request.state.form.get("wording")
        kind = runs.LETTER if request.state.form.get("kind") == runs.LETTER else runs.KIND
        latest = runs.latest(conn, queries.known(job_id), kind)
        if latest is None or latest["agreed"] != 0 or wording not in ("original", "writer"):
            raise HTTPException(404)
        with conn:
            conn.execute("UPDATE preparations SET wording = ? WHERE id = ?", (wording, latest["id"]))
        await rebuild(conn, latest["id"])
        return RedirectResponse(f"/jobs/{job_id}#preparation", 303)

    @app.post("/jobs/{job_id:int}/normal-price")
    async def normal_price(request: Request, job_id: int) -> Response:
        """The rest of the job at the normal price, sent at once (asked with a pop-up showing the estimate)."""
        conn = request.app.state.conn
        job = queries.job(conn, queries.known(job_id))
        if job is None:
            raise HTTPException(404)
        latest = runs.latest(conn, job_id)
        if latest and latest["status"] == "writing":
            with conn:
                conn.execute("UPDATE preparations SET mode = 'normal' WHERE id = ?", (latest["id"],))
        elif refused := prepare.cannot(conn, scoring(), job, prepare.waived_of(request)):
            return page(request, "error.html", 400, message=prepare.PROBLEMS[refused])
        else:
            flow.start(conn, load_config(), job_id, mode="normal", waived=prepare.waived_of(request))
        request.app.state.preparer.wake()
        return RedirectResponse(f"/jobs/{job_id}#preparation", 303)

    @app.post("/jobs/{job_id:int}/continue")
    async def continue_anyway(request: Request, job_id: int) -> Response:
        """A version stopped because the job closed or you skipped it carries on from its saved steps."""
        conn = request.app.state.conn
        kind = runs.LETTER if request.state.form.get("kind") == runs.LETTER else runs.KIND
        latest = runs.latest(conn, queries.known(job_id), kind)
        if latest is None or (doubt := drafts.DOUBT.get(latest["problem"])) is None or runs.writing(conn, job_id):
            raise HTTPException(404)
        waived = sorted({*json.loads(conn.execute("SELECT waived FROM preparations WHERE id = ?",
                                                  (latest["id"],)).fetchone()[0]), doubt})  # fmt: skip
        with conn:
            conn.execute(CONTINUE, (json.dumps(waived), latest["id"]))
        request.app.state.preparer.wake()
        return RedirectResponse(f"/jobs/{job_id}#preparation", 303)

    @app.post("/jobs/{job_id:int}/letter-changes")
    async def letter_changes(request: Request, job_id: int) -> Response:
        """Your words about the cover letter: a new letter built on the newest finished one, reviewed by its members;
        while one is being written, kept for its Writer's next round."""
        conn = request.app.state.conn
        job = queries.job(conn, queries.known(job_id))
        if job is None:
            raise HTTPException(404)
        words = " ".join(CONTROL.sub("", request.state.form.get("feedback", "")).split())
        latest, base = runs.latest(conn, job_id, runs.LETTER), runs.last_done(conn, job_id, runs.LETTER)
        if refused := prepare.cannot(conn, scoring(), job, prepare.waived_of(request)):
            problem = prepare.PROBLEMS[refused]
        elif not words or len(words) > prepare.MAX_FEEDBACK:
            problem = f"Type what you'd like changed (at most {prepare.MAX_FEEDBACK:,} characters)."
        elif latest is not None and latest["status"] == "writing":
            flow.ask(conn, latest["id"], words)
            problem = None
        elif base is None:
            problem = "There is no finished cover letter to change yet: press Write cover letter first."
        else:
            flow.start(conn, load_config(), job_id, runs.LETTER, words, prepare.waived_of(request))
            request.app.state.preparer.wake()
            problem = None
        if problem:
            return page(request, "error.html", 400, message=problem)
        return RedirectResponse(f"/jobs/{job_id}#preparation", 303)


async def rebuild(conn: sqlcipher3.Connection, preparation: int) -> None:
    """The version's PDF built again, apart from the page (up to minutes the first time). The old one is removed
    first, so a failed build never leaves a PDF in other wording to show."""
    config = load_config()
    try:
        target, source, note = runs.pdf_source(conn, config, preparation)
        target.unlink(missing_ok=True)
        target.with_suffix(".json").unlink(missing_ok=True)
        await run_in_threadpool(pdf.build_kept, config.data_dir, target, source)
        runs.note_pdf(conn, preparation, note)
    except (pdf.PdfError, privacy.PrivacyError, ResumeError, OSError) as error:
        runs.note_pdf(conn, preparation, error)


def _total(costs: list) -> float | None:
    return None if None in costs else sum(costs)
