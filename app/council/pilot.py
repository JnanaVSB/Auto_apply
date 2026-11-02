"""The pilot: the same jobs through several line-ups, to compare them (owner decisions D5 and 17; Part 4c update 3).

A pilot is 20 ready jobs from 20 companies, a round limit of 3 and your discussion turns, and 5 of its jobs get a
made-up claim slipped into the Writer's first draft (app/council/flow.py) to count how many each line-up's Truth
checker catches. Each line-up's versions are the pilot's own (kind "pilot"): never shown as your versions, never a
PDF; a result without a made-up claim can be copied into the job as a normal version."""

import json
import random
from datetime import UTC, datetime

import sqlcipher3

from app.config import Config
from app.council import flow, runs
from app.scoring import ready

SIZE, PLANTED, ROUNDS = 20, 5, 3
CLAIMS = (  # made-up, and without digits so the "new number" flag can't give them away (owner choice P9-1)
    "earning the team's Outstanding Contributor award",
    "work later adopted as the company-wide standard",
    "leading a team of five engineers",
    "presented as an invited talk at a major conference",
    "for which a patent was granted",
)
VERSIONS = """SELECT id, job_id, status, agreed, round, sources, draft, planted, created_on, finished_on
    FROM preparations WHERE pilot_id = ? AND lineup = ?"""
STEPS = """SELECT id, round, member, turn, status, answer, input_tokens + cached_tokens, output_tokens, cost
    FROM steps WHERE preparation_id = ?"""
MARKS = "SELECT step_id, objection, useful FROM objection_marks"
COPY = """INSERT INTO preparations (job_id, kind, version, sources, writer, council, status, round, agreed, wording,
    draft, flags, mode, created_on, finished_on) SELECT job_id, 'resume', (SELECT coalesce(max(version), 0) + 1 FROM
    preparations WHERE job_id = p.job_id AND kind = 'resume'), sources, writer, council, 'done', round, agreed,
    wording, draft, flags, mode, ?, ? FROM preparations p
    WHERE id = ? AND kind = 'pilot' AND status = 'done' AND planted IS NULL"""


def suggest(conn: sqlcipher3.Connection, config: Config, resume: str | None) -> list[int]:
    """The newest ready jobs, one per company, at most SIZE."""
    found, companies = [], set()
    for job_id in ready.newest(conn, config.ai.evaluator.model, config.scoring.ready_threshold, resume, 500):
        company = conn.execute("SELECT lower(company) FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
        if company not in companies:
            found.append(job_id)
            companies.add(company)
        if len(found) == SIZE:
            break
    return found


def create(conn: sqlcipher3.Connection, config: Config, jobs: list[int], seed: int | None = None) -> int:
    """A new pilot over these jobs; PLANTED of them, chosen at random, get a made-up claim each."""
    chosen = random.Random(seed).sample(jobs, min(PLANTED, len(jobs)))  # noqa: S311 (a fair pick, not a secret)
    planted = {str(job_id): claim for job_id, claim in zip(chosen, CLAIMS, strict=False)}
    with conn:
        return conn.execute(
            "INSERT INTO pilots (jobs, planted, rounds, turns, created_on) VALUES (?, ?, ?, ?, ?)",
            (json.dumps(jobs), json.dumps(planted), ROUNDS, config.council.turns, _now()),
        ).lastrowid


def run(conn: sqlcipher3.Connection, config: Config, pilot: int, lineup: str, roles: dict) -> int:
    """Starts the line-up (its models by member) on every job of the pilot it hasn't run yet; returns how many."""
    jobs = json.loads(conn.execute("SELECT jobs FROM pilots WHERE id = ?", (pilot,)).fetchone()[0])
    done = {job_id for _, job_id, *_ in conn.execute(VERSIONS, (pilot, lineup))}
    for job_id in jobs:
        if job_id not in done:
            flow.start(conn, config, job_id, pilot=(pilot, lineup, roles))
    return len(set(jobs) - done)


def summary(conn: sqlcipher3.Connection, pilot: int, lineup: str) -> dict:
    """One line-up's results on the pilot: how its jobs ended, rounds, time, tokens and dollars per job, made-up claims
    caught (a Truth checker "must" on that entry in round 1: owner choice P9-2) and still in the final wording, your
    useful marks, and the Writer's missed replies and renumbered objections."""
    marks = {(step, objection): useful for step, objection, useful in conn.execute(MARKS)}
    found = {"jobs": 0, "finished": 0, "agreed": 0, "no_agreement": 0, "by_hand": 0, "rounds": 0, "minutes": 0.0,
             "tokens": 0, "cost": 0.0, "planted": 0, "caught": 0, "kept": 0, "useful": 0, "not_useful": 0,
             "missed": 0, "missed_must": 0, "renumbered": 0}  # fmt: skip
    for preparation, _, status, agreed, rounds, sources, draft, planted, created, finished in conn.execute(
        VERSIONS, (pilot, lineup)
    ).fetchall():
        found["jobs"] += 1
        steps = conn.execute(STEPS, (preparation,)).fetchall()
        found["tokens"] += sum(row[6] + row[7] for row in steps)
        found["cost"] += sum(row[8] or 0 for row in steps)
        for step, _, member, _, state, answer, *_ in steps:
            answer = json.loads(answer) if state == "done" and answer else {}
            if member == "writer":
                missed = answer.get("draft", {}).get("missed", [])
                found["missed"] += len(missed)
                found["missed_must"] += sum(item["severity"] == "must" for item in missed)
            else:
                found["renumbered"] += len(answer.get("renumbered", []))
                for item in answer.get("objections", []):
                    if (step, item["id"]) in marks:
                        found["useful" if marks[(step, item["id"])] else "not_useful"] += 1
        if planted:
            found["planted"] += 1
            found["caught"] += _caught(conn, steps, sources, json.loads(planted))
            found["kept"] += bool(draft) and json.loads(planted)["claim"] in draft
        if status != "writing":
            found["finished"] += 1
            found["rounds"] += rounds
            found["agreed" if agreed == 1 else "no_agreement" if agreed == 0 else "by_hand"] += 1
            minutes = datetime.fromisoformat(finished) - datetime.fromisoformat(created)
            found["minutes"] += minutes.total_seconds() / 60
    if found["finished"]:
        found["rounds"] /= found["finished"]
        found["minutes"] /= found["finished"]
    if found["jobs"]:
        found["tokens"] //= found["jobs"]
        found["cost"] /= found["jobs"]
    return found


def results(conn: sqlcipher3.Connection, pilot: int, lineup: str, job_id: int) -> dict | None:
    """One line-up's result for one job: its version, final draft, made-up claim, and every reviewer's objections
    (with the step they came from, for your useful marks)."""
    row = conn.execute(VERSIONS + " AND job_id = ?", (pilot, lineup, job_id)).fetchone()
    if row is None:
        return None
    preparation, _, status, agreed, rounds, _, draft, planted, *_ = row
    marks = {(step, objection): useful for step, objection, useful in conn.execute(MARKS)}
    objections = [
        {"step": step, "round": number, "member": member, "turn": turn, "marked": marks.get((step, item["id"]))} | item
        for step, number, member, turn, state, answer, *_ in conn.execute(STEPS, (preparation,))
        if member != "writer" and state == "done" and answer
        for item in json.loads(answer)["objections"]
    ]  # fmt: skip
    return {
        "id": preparation, "status": status, "agreed": agreed, "rounds": rounds, "objections": objections,
        "draft": json.loads(draft) if draft else None, "planted": json.loads(planted) if planted else None,
    }  # fmt: skip


def mark(conn: sqlcipher3.Connection, step: int, objection: str, useful: bool) -> None:
    """Your mark on one objection: useful or not (marking again changes it)."""
    with conn:
        conn.execute(
            "INSERT INTO objection_marks (step_id, objection, useful, marked_on) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (step_id, objection) DO UPDATE SET useful = excluded.useful, marked_on = excluded.marked_on",
            (step, objection, int(useful), _now()),
        )


def use(conn: sqlcipher3.Connection, preparation: int) -> int | None:
    """A finished pilot result without a made-up claim, copied into its job as your newest version (None: it can't
    be: not finished, or it holds a made-up claim). Its PDF is built by the caller."""
    with conn:
        copied = conn.execute(COPY, (_now(), _now(), preparation))
    return copied.lastrowid if copied.rowcount == 1 else None


def _caught(conn: sqlcipher3.Connection, steps: list, sources: str, planted: dict) -> bool:
    labels = {(entry.source, entry.tile.tile_id): entry.label for entry in runs.originals(conn, sources)}
    label = labels.get((planted["source"], planted["tile_id"]))
    for _, number, member, _, state, answer, *_ in steps:
        if number == 1 and member == "truth" and state == "done" and answer:
            for item in json.loads(answer)["objections"]:
                if item["severity"] == "must" and item["where"] in (label, f"{label}.{planted['bullet'] + 1}"):
                    return True
    return False


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
