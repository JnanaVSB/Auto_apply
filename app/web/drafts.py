"""What the Writer made, kept apart from main.py and prepare.py: a job's state on the Ready pages, its tailored PDF,
building that PDF again, the "changes vs your master" page, the Usage page and the Failed tries page."""

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import sqlcipher3
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from app import questionnaire
from app.config import REVIEWERS, AIRole, Config, ConfigError, SpendingSettings, load_config
from app.council import estimate, flow, reviewers, runs, spending, writer
from app.documents import pdf, privacy
from app.resume import ResumeError
from app.web import queries

STEPS = "SELECT round, member, turn, status, answer, mode, retry_after FROM steps WHERE preparation_id = ?"

SPENT = "SELECT coalesce(sum(coalesce(cost, 0) + reserved), 0) FROM steps WHERE preparation_id = ?"

DOUBT = {flow.CLOSED: "closed", flow.SKIPPED: "skipped"}  # a version stopped for this can continue anyway
SHOWN = {"day": 60, "week": 26, "month": 24, "year": None}  # how many of each the Usage page lists; None: all


def state(conn: sqlcipher3.Connection, data_dir: Path, job_id: int) -> dict:
    """What the Ready page and the job's box show: the newest version (and whether it failed), whether one is being
    written (place 0), what
    the PDF build found, the cost so far, and whether Prepare would write a new version."""
    latest, current = runs.latest(conn, job_id), runs.current(conn, job_id)
    done = latest is not None and latest["status"] == "done"
    base = latest if done else runs.last_done(conn, job_id)
    return {
        "base": base, "changeable": base is not None and not runs.outdated(base, current),
        "latest": latest, "failed": latest is not None and latest["status"] in runs.FAILED,
        "alone": latest is not None and runs.alone(latest),
        "place": 0 if runs.writing(conn, job_id) else None, "outdated": done and runs.outdated(latest, current),
        "built": pdf.built(pdf.tailored(data_dir, job_id, latest["version"])) if done else None,
        "cost": spending.of_job(conn, job_id), "writable": runs.writable(latest, current),
        "council_status": status(conn, latest, _rounds()),
    }  # fmt: skip


def letter(conn: sqlcipher3.Connection, data_dir: Path, job_id: int) -> dict:
    """The job's cover letters for its box: the newest, its text in parts (new numbers marked), the paragraphs the
    Truth checker still objects to after no agreement, its PDF, every version."""
    latest = runs.latest(conn, job_id, runs.LETTER)
    done = latest is not None and latest["status"] == "done"
    marked = {flag["paragraph"]: set(flag["numbers"]) for flag in json.loads(latest["flags"])} if done else {}
    paragraphs = json.loads(latest["draft"])["paragraphs"] if done else []
    left_out = json.loads(latest["draft"]).get("objected", []) if done else []  # only after no agreement
    return {
        "latest": latest, "versions": runs.versions(conn, job_id, runs.LETTER),
        "alone": latest is not None and runs.alone(latest),
        "paragraphs": [_parts(text, marked.get(number, set())) for number, text in enumerate(paragraphs)],
        "left_out": left_out,
        "built": pdf.built(pdf.tailored(data_dir, job_id, latest["version"], runs.LETTER)) if done else None,
    }  # fmt: skip


def add_routes(app: FastAPI, page: Callable[..., HTMLResponse], data_dir: Path) -> None:
    @app.get("/documents/{job_id:int}/resume-v{version:int}.pdf")
    async def tailored_pdf(job_id: int, version: int) -> Response:
        return _pdf(pdf.tailored(data_dir, job_id, version))  # whole numbers only: never a path from the address bar

    @app.get("/documents/{job_id:int}/cover-letter-v{version:int}.pdf")
    async def letter_pdf(job_id: int, version: int) -> Response:
        return _pdf(pdf.tailored(data_dir, job_id, version, runs.LETTER))

    @app.post("/jobs/{job_id:int}/pdf")
    async def build_again(request: Request, job_id: int) -> Response:
        """The newest version's PDF again (the resume, or the cover letter), e.g. after a failed build or a changed
        Questionnaire contact."""
        kind = runs.LETTER if request.state.form.get("kind") == runs.LETTER else runs.KIND
        conn, latest = request.app.state.conn, runs.latest(request.app.state.conn, queries.known(job_id), kind)
        if latest is None or latest["status"] != "done":
            raise HTTPException(404)
        try:
            target, source, note = runs.pdf_source(conn, load_config(), latest["id"])
            await run_in_threadpool(pdf.build_kept, data_dir, target, source)  # up to minutes the first time
            runs.note_pdf(conn, latest["id"], note)
        except (pdf.PdfError, privacy.PrivacyError, ResumeError, ConfigError) as error:
            runs.note_pdf(conn, latest["id"], error)
        return RedirectResponse(f"/jobs/{job_id}", 303)

    @app.get("/jobs/{job_id:int}/changes")
    async def changes(request: Request, job_id: int) -> Response:
        """One version beside your master: each entry's original wording and the Writer's, new numbers marked."""
        conn, number = request.app.state.conn, request.query_params.get("version", "")
        version = runs.version(conn, queries.known(job_id), int(number) if queries.PAGE_NUMBER.fullmatch(number) else 0)
        if version is None or version["draft"] is None:  # no such version, or no draft
            raise HTTPException(404)
        entries, draft = runs.originals(conn, version["sources"]), json.loads(version["draft"])
        try:
            restore = runs.put_back(conn, load_config(), entries, draft, questionnaire.load(conn)[0]["contact"])[0]
        except (privacy.PrivacyError, ResumeError, ConfigError):  # shown as the Writer saw it, placeholders and all
            restore = str
        return page(
            request, "changes.html", job=queries.job(conn, job_id), version=version,
            rows=_rows(entries, draft, json.loads(version["flags"]), restore),
            notes=draft["notes"], model=json.loads(version["writer"])["model"], wording=version["wording"],
            built=pdf.built(pdf.tailored(data_dir, job_id, version["version"])),
        )  # fmt: skip

    @app.get("/usage")
    async def usage(request: Request) -> Response:
        """Your AI use by day, week, month or year, and today's and this month's spend against your limits."""
        conn, by = request.app.state.conn, request.query_params.get("by", "day")
        by = by if by in SHOWN else "day"
        try:
            limits = load_config().spending
        except ConfigError:  # shown on the Settings page; the defaults meanwhile
            limits = SpendingSettings()
        return page(
            request, "usage.html", by=by, periods=list(SHOWN), rows=spending.history(conn, by)[: SHOWN[by]],
            spent=spending.spent(conn, spending.today()), limits=limits,
        )  # fmt: skip

    @app.get("/failed-tries")
    async def failed_tries(request: Request) -> Response:
        """The Writer's failed tries with what came back, for troubleshooting: newest first, a page at a time; with
        ?version=, only that version's."""
        number, version = (request.query_params.get(name, "") for name in ("page", "version"))
        number = max(1, int(number)) if queries.PAGE_NUMBER.fullmatch(number) else 1
        version = int(version) if queries.PAGE_NUMBER.fullmatch(version) else None
        size = queries.PAGE_SIZE
        rows = runs.failed_tries(request.app.state.conn, size + 1, (number - 1) * size, version)  # one more: a next?
        return page(
            request, "failed_tries.html", rows=rows[:size], more=len(rows) > size, number=number, version=version,
            kinds={runs.KIND: "Resume", runs.LETTER: "Cover letter"},
        )  # fmt: skip


def _rounds() -> int:
    try:
        return load_config().council.rounds
    except ConfigError:  # shown on the Settings page; the default meanwhile
        return 3


def _pdf(path: Path) -> Response:
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="application/pdf", filename=path.name, content_disposition_type="inline")


def _rows(entries: list[writer.Entry], draft: dict, flags: list[dict], restore: Callable[[str], str]) -> list[dict]:
    """Each entry of the version's sources in your resume's order: its original and what the Writer made of it
    (None: left out), each new bullet in parts, the new numbers marked."""
    chosen = {(item["source"], item["tile_id"]): item for item in draft["entries"]}
    marked = {(flag["source"], flag["tile_id"], flag["bullet"]): set(flag["numbers"]) for flag in flags}
    rows = []
    for entry in entries:
        key, tile = (entry.source, entry.tile.tile_id), entry.tile
        item = chosen.get(key)
        bullets = [
            {"parts": _parts(restore(bullet["text"]), marked.get((*key, number), set())), "from": bullet["from"]}
            for number, bullet in enumerate(item["bullets"] if item else [])
        ]
        rows.append({
            "tile": tile, "chosen": item is not None, "bullets": bullets, "as_written": writer.kept_as_written(tile),
            "objected": list(key) in draft.get("objected", []),
            "skills": [restore(skill) for skill in item["skills"]] if item else [],
        })  # fmt: skip
    return rows


def _parts(text: str, numbers: set[str]) -> list[tuple[str, bool]]:
    """The text in pieces, each new number a piece of its own marked True (the page highlights it)."""
    parts, last = [], 0
    for match in writer.NUMBER.finditer(text):
        if match[0] in numbers:
            parts += [(text[last : match.start()], False), (match[0], True)]
            last = match.end()
    return [part for part in [*parts, (text[last:], False)] if part[0]]


def status(conn: sqlcipher3.Connection, version: dict | None, rounds: int) -> str | None:
    """The line the Ready page and the box show for a version being written or done (None: as before)."""
    if version is None or version["status"] not in ("writing", "done"):
        return None
    members = json.loads(version["council"] or '{"members": {}}')["members"]
    if version["status"] == "done":
        return {1: "council agreed", 0: "no agreement: see the job"}.get(version["agreed"], "Writer only (no council)")
    if (version["problem"] or "").startswith(("Paused", "Waiting")):
        return version["problem"].split(" (")[0]
    held = [(row[5], row[6]) for row in conn.execute(STEPS, (version["id"],)) if row[3] in ("sent", "waiting")]
    if any(mode == "batch" for mode, _ in held):
        return "waiting for Anthropic"
    if after := max((after for _, after in held if after), default=None):
        return f"waiting: busy, again at {datetime.fromisoformat(after).astimezone():%H:%M}"
    return f"council round {version['round']} of {rounds}" if members else "Writer writing"


def notes(conn: sqlcipher3.Connection, version: dict | None) -> dict | None:
    """A version's council notes from its last round: each member's verdict and objections with the votes on them
    (the Truth checker first), the Writer's replies, what it left unanswered and what the app renumbered."""
    council = json.loads((version or {}).get("council") or '{"members": {}, "names": {}}')
    if not council["members"]:
        return None
    rows = conn.execute(STEPS, (version["id"],)).fetchall()
    last = max((row[0] for row in rows), default=1)
    names = {member: reviewers.name(member, council["names"]) for member in council["members"]}
    members = []
    for member in sorted(council["members"], key=lambda name: name != "truth"):
        found = max((row for row in rows if row[0] == last and row[1] == member), key=lambda row: row[2], default=None)
        answer = json.loads(found[4]) if found and found[3] == "done" else None
        state = "waiting" if found is None or found[3] in ("sent", "waiting") else found[3]
        members.append({"name": names[member], "truth": member == "truth", "answer": answer, "state": state})
    votes: dict[str, list] = {}
    for found in members:
        for vote in (found["answer"] or {}).get("votes", []):
            votes.setdefault(vote["objection"], []).append((found["name"], vote["agree"], vote["why"]))
    writer = next((json.loads(row[4]) for row in rows if row[0] == last and row[1] == "writer" and row[3] == "done"
                   and row[4]),
                  {})  # fmt: skip
    draft = writer.get("draft", {})
    renumbered = [(found["name"], found["answer"]["renumbered"]) for found in members
                  if (found["answer"] or {}).get("renumbered")]  # fmt: skip
    return {"round": last, "members": members, "votes": votes, "replies": draft.get("responses", []),
            "missed": draft.get("missed", []), "renumbered": renumbered}  # fmt: skip


def council(conn: sqlcipher3.Connection, job_id: int) -> dict:
    """The job box's council part: notes, the status line, the wording switch, and Now, normal price's estimate."""
    try:
        config = load_config()
    except ConfigError:
        return {}
    latest, letter = runs.latest(conn, job_id), runs.latest(conn, job_id, runs.LETTER)
    objected, no_truth = [], False
    if latest and latest["status"] == "done" and latest["agreed"] == 0:
        entries, draft = runs.originals(conn, latest["sources"]), json.loads(latest["draft"])
        labels = {(entry.source, entry.tile.tile_id): entry.tile.facts.get("name") or entry.label for entry in entries}
        objected = [labels.get(tuple(key), "an entry") for key in draft.get("objected", [])]
        no_truth = draft.get("no_truth", False)  # the Truth checker gave no valid answer: these are unchecked
    return {
        "council_notes": notes(conn, latest),
        "letter_notes": notes(conn, letter), "objected": objected, "normal_cost": _normal(conn, config, latest),
        "no_truth": no_truth, "doubt": DOUBT.get(latest and latest["problem"]),
    }  # fmt: skip


def _normal(conn: sqlcipher3.Connection, config: Config, latest: dict | None) -> float | None:
    """What the rest of the job would cost at the normal price: the version being written less what it has spent, or
    a whole new version with today's members."""
    try:
        if latest and latest["status"] == "writing":
            writer = AIRole(**json.loads(latest["writer"]))
            council = json.loads(latest["council"] or '{"members": {}}')
            members = {name: AIRole(**role) for name, role in council["members"].items()}
            spent = conn.execute(SPENT, (latest["id"],)).fetchone()[0]
        else:
            members = {m: getattr(config.ai, m) for m in REVIEWERS} | {a.id: a.role for a in config.ai.extra}
            writer, spent = config.ai.writer, 0
        if writer is None or None in members.values():
            return None
        full = estimate.version(conn, writer, members, config.council.rounds, config.council.turns, half=False)
    except (TypeError, ValueError):
        return None
    return None if full is None else max(full - spent, 0)
