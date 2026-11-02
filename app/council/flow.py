"""The council's rounds for one version of a job's resume or cover letter. What a version needs next is worked out each
time from its saved steps (app/council/runs.py's tables), so a crash, a closed window or a sleeping laptop loses
nothing; sending the calls and saving their answers is the runner's job (app/council/runner.py).

A round: the Writer writes (round 1) or revises from the council's update; every reviewer reviews it alone (turn 0);
then up to `turns` discussion turns, stopping early once nobody changes anything. All approve with a valid answer:
agreed. Otherwise another round, up to the round limit (one more when your words came too late for the last Writer);
then no agreement, where entries the Truth checker still objects to keep your original wording (a switch on the job
changes that). Before every new step the job is checked: skipped, closed or made from older entries stops it."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

import sqlcipher3

from app import questionnaire
from app.config import REVIEWERS, AIRole, Config
from app.council import letter, reviewers, runs, writer
from app.documents import pdf, privacy, writer_sources
from app.resume import ResumeError, read_resume

MOST_ROUNDS = 6  # the round limit is at most 5; one more when your words came after the last Writer
WAITING = ("sent", "waiting")
START = """INSERT INTO preparations (job_id, kind, version, based_on, feedback, sources, writer, council, waived, mode,
    automatic, pilot_id, lineup, status, created_on) VALUES (?, ?, (SELECT coalesce(max(version), 0) + 1 FROM
    preparations WHERE job_id = ? AND kind = ?), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'writing', ?)"""
VERSION = """SELECT p.id, p.job_id, p.kind, p.version, p.based_on, p.feedback, p.asked, p.sources, p.writer, p.council,
    p.waived, p.round, p.status, p.draft, j.status, j.not_interested, b.draft, b.version, t.rounds, t.turns, t.planted,
    p.planted FROM preparations p JOIN jobs j ON j.id = p.job_id LEFT JOIN preparations b ON b.id = p.based_on
    LEFT JOIN pilots t ON t.id = p.pilot_id WHERE p.id = ?"""
STEPS = "SELECT round, member, turn, status, answer, sent_on FROM steps WHERE preparation_id = ?"
ROUND = "UPDATE preparations SET round = ? WHERE id = ?"
DRAFT = "UPDATE preparations SET draft = ?, flags = ? WHERE id = ?"
ENDED = """UPDATE preparations SET status = ?, problem = ?, agreed = ?, wording = ?, draft = coalesce(?, draft),
    finished_on = ? WHERE id = ?"""
CLOSED = "Job closed: preparation stopped. Steps already sent were finished and saved."
SKIPPED = "You skipped this job: preparation stopped. Steps already sent were finished and saved."
CHANGED = "Your entries changed: preparation stopped. It starts again once you confirm them."
NO_WRITER = "The Writer gave no valid answer twice in round {}: check by hand, or press Retry."
ONE_PAGE = "The resume's PDF is {} pages: it must fit on one page."
LETTER_UNCLEAR = ("No agreement: the Truth checker still objects to the letter as a whole or to an entry, so no "
                  "paragraph can be left out. Check it by hand.")  # fmt: skip


class Call(NamedTuple):
    """One AI call a version needs now. `check` turns the answer into what its step keeps, or raises BadAnswer."""

    preparation: int
    round: int
    member: str
    turn: int
    role: AIRole
    system: str
    user: str
    shape: dict
    limit: int
    timeout: int
    check: Callable[[str], dict]


def start(
    conn: sqlcipher3.Connection, config: Config, job_id: int, kind: str = runs.KIND, feedback: str = "",
    waived: frozenset[str] = frozenset(), mode: str = "half", automatic: bool = False, writer_only: bool = False,
    pilot: tuple[int, str, dict] | None = None,
) -> int:  # fmt: skip
    """A new version of the job's resume (or cover letter), waiting for its first step, with the Writer's and the
    reviewers' settings as they are now (a later change in Settings applies to new versions only). A change (your
    words) builds on the newest finished version. `writer_only`: "Resume only", no council. `pilot`: a pilot's
    version (its id, the line-up's name, and the line-up's models by member), kept apart from yours."""
    if pilot:  # a pilot's version: only its line-up's models, whatever Settings say
        kind, roles = "pilot", pilot[2]
        writer, council = json.dumps(roles["writer"]), {"members": {m: roles[m] for m in REVIEWERS}, "names": {}}
    else:
        added = [] if writer_only else [m for m in config.ai.extra if kind != runs.LETTER or m.letters]
        council = {
            "members": {member: getattr(config.ai, member).model_dump() for member in REVIEWERS
                        if not writer_only and (kind != runs.LETTER or member in reviewers.LETTER_MEMBERS)}
            | {member.id: member.role.model_dump() for member in added},
            "names": {member.id: member.name for member in added},
        }  # fmt: skip
        writer = config.ai.writer.model_dump_json()
    base = runs.last_done(conn, job_id, kind) if feedback else None
    with conn:
        return conn.execute(START, (
            job_id, kind, job_id, kind, base and base["id"], feedback or None, json.dumps(runs.current(conn, job_id)),
            writer, json.dumps(council), json.dumps(sorted(waived)), mode, int(automatic), pilot and pilot[0],
            pilot and pilot[1], _now(),
        )).lastrowid  # fmt: skip


def ask(conn: sqlcipher3.Connection, preparation: int, words: str) -> None:
    """Your words sent while the version is being written: used from the Writer's next round on (owner choice D)."""
    with conn:
        asked = json.loads(conn.execute("SELECT asked FROM preparations WHERE id = ?", (preparation,)).fetchone()[0])
        conn.execute("UPDATE preparations SET asked = ? WHERE id = ?",
                     (json.dumps([*asked, {"words": words, "on": _now()}]), preparation))  # fmt: skip


def advance(conn: sqlcipher3.Connection, config: Config, preparation: int) -> list[Call]:
    """The calls the version needs now (none while it waits for answers, or once it has ended)."""
    version = _version(conn, preparation)
    if version["status"] != "writing":
        return []
    if problem := _stopped(conn, config, version):
        end(conn, preparation, "stopped", problem)
        return []
    try:
        return _next(conn, config, version)
    except (privacy.PrivacyError, ResumeError, reviewers.NoInstructions) as error:  # nothing sent: fix it, Retry
        end(conn, preparation, "stopped", str(error))
    except privacy.PrivacyStop as stop:
        end(conn, preparation, "stopped", f"Privacy check stopped this: {stop}. Change that, then try again.")
    return []


def pending(conn: sqlcipher3.Connection, config: Config, preparation: int) -> list[Call]:
    """The calls already sent (or waiting to be sent again) that still need their answer, built again exactly as they
    were sent, so their answers can be checked when they come (an Anthropic batch, a busy service). Whatever the
    version's status: steps already sent always finish and are saved."""
    try:
        return _next(conn, config, _version(conn, preparation), waiting=True)
    except (privacy.PrivacyError, privacy.PrivacyStop, ResumeError, reviewers.NoInstructions):
        return []


def _next(conn: sqlcipher3.Connection, config: Config, version: dict, waiting: bool = False) -> list[Call]:
    """The calls not yet sent; or, `waiting`, the ones sent and still unanswered."""
    preparation, number = version["id"], version["round"]
    steps = {(row[0], row[1], row[2]): row[3:] for row in conn.execute(STEPS, (preparation,))}
    seen = _Seen.of(conn, config, version)
    written = steps.get((number, "writer", 0))
    if written is None or written[0] in WAITING:
        return _writer(seen, version, steps, number) if (written is None) is not waiting else []
    if written[0] != "done":
        if not waiting:
            end(conn, preparation, "by_hand", NO_WRITER.format(number))
        return []
    found = json.loads(written[1])
    if number == 1 and version["claims"] and not version["planted"] and not waiting:
        found = _plant(conn, version, found)
    _keep(conn, config, version, found)
    if not seen.members:  # "Resume only": no council
        if not waiting:
            end(conn, preparation, "done", None)
            _build(conn, config, preparation)
        return []
    answers, before = None, None
    rounds, turns = version["pilot_rounds"] or config.council.rounds, version["pilot_turns"]
    for turn in range((config.council.turns if turns is None else turns) + 1):
        if turn and not _discuss(answers, before, turn):
            break
        rows = {member: steps.get((number, member, turn)) for member in seen.members}
        missing = [member for member, row in rows.items() if row is None]
        held = [member for member, row in rows.items() if row is not None and row[0] in WAITING]
        if missing or held:
            return [_review(seen, version, steps, found, number, member, turn, answers)
                    for member in (held if waiting else missing)]  # fmt: skip
        before, answers = answers, {m: json.loads(row[1]) if row[0] == "done" else None for m, row in rows.items()}
    if waiting:
        return []
    late = [asked for asked in json.loads(version["asked"]) if asked["on"] > written[2]]  # after this round's Writer
    if all(answer and answer["verdict"] == "approve" for answer in answers.values()) and not late:
        end(conn, preparation, "done", None, agreed=True)
        _build(conn, config, preparation)
        return []
    if number < rounds or (late and number < MOST_ROUNDS):
        with conn:
            conn.execute(ROUND, (number + 1, preparation))
        return advance(conn, config, preparation)
    _no_agreement(conn, config, version, found["draft"], _final(steps, number, seen.members), seen)
    return []


class _Seen(NamedTuple):
    """What the version's calls are made from: the job, your entries as recorded, the privacy filter, the members."""

    job: tuple[str, str, str, str]
    entries: list[writer.Entry]
    shield: privacy.Shield
    letter: bool
    members: dict[str, AIRole]
    names: dict[str, str]
    data_dir: Path
    one_page: bool

    @classmethod
    def of(cls, conn: sqlcipher3.Connection, config: Config, version: dict) -> "_Seen":
        title, company, locations, description = conn.execute(runs.JOB, (version["job_id"],)).fetchone()
        entries = runs.originals(conn, version["sources"])
        if version["kind"] == runs.LETTER:
            resume = runs.last_done(conn, version["job_id"])
            fresh = resume is not None and not runs.outdated(resume, json.loads(version["sources"]))
            keys = (
                {(item["source"], item["tile_id"]) for item in json.loads(resume["draft"])["entries"]}
                if fresh
                else None
            )
            entries = letter.chosen(entries, keys)
        shield = runs.shield_for(conn, config, entries, questionnaire.load(conn)[0]["contact"])
        council = json.loads(version["council"])
        names = {member: reviewers.name(member, council["names"]) for member in council["members"]}
        return cls(
            (title, company, ", ".join(json.loads(locations)), description or ""), entries, shield,
            version["kind"] == runs.LETTER, {member: AIRole(**role) for member, role in council["members"].items()},
            names, config.data_dir, config.documents.one_page,
        )  # fmt: skip


def _writer(seen: _Seen, version: dict, steps: dict, number: int) -> list[Call]:
    """The Writer's call: a first draft (or your change to the newest finished version), or a revision from the last
    round's council update, with your words and the page count."""
    previous, council, objections, asked = None, "", None, _asked(version)
    if number > 1:
        answers = _final(steps, number - 1, seen.members)
        previous, council = (
            json.loads(steps[(number - 1, "writer", 0)][1])["draft"],
            reviewers.update(answers, seen.names),
        )
        objections, asked = reviewers.objections(answers), " ".join([asked, _pages(seen, version)]).strip()
    elif version["based_on"]:
        previous, asked = json.loads(version["base"]), " ".join([asked, _pages(seen, version, "base_version")]).strip()
    module = letter if seen.letter else writer
    system, user = module.request(*seen.job, seen.entries, seen.shield, previous, asked if previous else "", council)

    def check(text: str) -> dict:
        if seen.letter:
            found = letter.checked(text, seen.entries, seen.job[3], objections)
        else:
            found = writer.checked(text, seen.entries, seen.shield, objections)
        return {"draft": found.draft, "flags": found.flags}

    return [Call(version["id"], number, "writer", 0, AIRole(**json.loads(version["writer"])), system, user,
                 module.SHAPE, writer.ANSWER_LIMIT, writer.TIMEOUT_SECONDS, check)]  # fmt: skip


def _review(
    seen: _Seen, version: dict, steps: dict, found: dict, number: int, member: str, turn: int, previous: dict | None
) -> Call:
    """One reviewer's call: its own review (turn 0) or a discussion turn, with the last round's update and the
    Writer's replies to it."""
    debate = ""
    if number > 1:
        update = reviewers.update(_final(steps, number - 1, seen.members), seen.names)
        debate = "\n\n".join(part for part in (update, reviewers.replies(found["draft"])) if part)
    pages = _pages(seen, version)
    flags = _flags(seen, found["flags"]) + ([pages] if pages else [])
    shown = reviewers.shown(seen.job, seen.entries, seen.shield, found["draft"], seen.letter, flags, debate,
                            _asked(version))  # fmt: skip
    where = reviewers.places(seen.entries, seen.shield, found["draft"], seen.letter)
    skill = reviewers.instructions(member, seen.data_dir)
    system, user = reviewers.request(member, skill, seen.letter, shown, previous, seen.names, seen.shield)
    return Call(version["id"], number, member, turn, seen.members[member], system, user, reviewers.SHAPE,
                reviewers.ANSWER_LIMIT, reviewers.TIMEOUT_SECONDS,
                lambda text: reviewers.checked(text, member, where, previous))  # fmt: skip


def _discuss(answers: dict, before: dict | None, turn: int) -> bool:
    """Whether another discussion turn is needed: after the reviews, unless all approve with nothing to say; after a
    discussion turn, only if somebody changed something."""
    if turn == 1:
        return not all(answer and answer["verdict"] == "approve" and not answer["objections"]
                       for answer in answers.values())  # fmt: skip
    return any(reviewers.changed(before[member], answers[member]) for member in answers)


def _final(steps: dict, number: int, members: dict) -> dict[str, dict | None]:
    """Each member's last valid answer in a round: a discussion turn without one leaves its earlier answer standing
    (None: no valid answer in any turn)."""
    found = {}
    for member in members:
        turns = [(turn, row[1]) for (round_, who, turn), row in steps.items()
                 if round_ == number and who == member and row[0] == "done"]  # fmt: skip
        found[member] = json.loads(max(turns)[1]) if turns else None
    return found


def _no_agreement(conn: sqlcipher3.Connection, config: Config, version: dict, draft: dict, answers: dict,
                  seen: _Seen) -> None:  # fmt: skip
    """No agreement at the round limit, from each member's last valid answer in the round. A resume: entries the
    Truth checker still objects to keep your original wording (all rewritten ones for an objection to the whole
    resume); with no valid answer from it in the whole round, the Writer's wording stays, with a warning. A letter:
    objected paragraphs are left out, or it is checked by hand."""
    truth = answers.get("truth")
    musts = [item["where"] for item in (truth or {}).get("objections", []) if item["severity"] == "must"]
    labels = {entry.label: (entry.source, entry.tile.tile_id) for entry in seen.entries}
    if seen.letter:
        if truth is None or any(not where.startswith("P") for where in musts):
            end(conn, version["id"], "by_hand", LETTER_UNCLEAR, agreed=False)
            return
        objected, wording = sorted({int(where[1:]) - 1 for where in musts}), "original" if musts else None
    elif truth is None or "" in musts:
        chosen = [[item["source"], item["tile_id"]] for item in draft["entries"] if item["bullets"]]
        objected, wording = chosen, "writer" if truth is None else "original"
    else:
        objected = [list(labels[label]) for label in dict.fromkeys(where.split(".")[0] for where in musts)]
        wording = "original" if objected else None
    end(conn, version["id"], "done", None, agreed=False, wording=wording,
        draft=draft | {"objected": objected, "no_truth": truth is None})  # fmt: skip
    _build(conn, config, version["id"])


def _keep(conn: sqlcipher3.Connection, config: Config, version: dict, found: dict) -> None:
    """The newest Writer draft becomes the version's draft (and its resume PDF is built, for the page count), once."""
    draft = json.dumps(found["draft"], ensure_ascii=False)
    if version["draft"] == draft:
        return
    with conn:
        conn.execute(DRAFT, (draft, json.dumps(found["flags"]), version["id"]))
    version["draft"] = draft
    if version["kind"] != runs.LETTER:  # a letter has no page limit
        _build(conn, config, version["id"])


def _plant(conn: sqlcipher3.Connection, version: dict, found: dict) -> dict:
    """A pilot's made-up claim, slipped into the first bullet of the first job or project in the Writer's first
    draft before the reviewers read it (owner choice 17 b): the Truth checker should catch it. Recorded with the
    version; never in a PDF."""
    claim = json.loads(version["claims"]).get(str(version["job_id"]))
    written = [item for item in found["draft"]["entries"] if item["bullets"]]
    item = next(
        (item for item in written if item["tile_id"].startswith(("exp", "proj"))), written[0] if written else None
    )
    if claim is None or item is None:
        return found
    bullet = item["bullets"][0]
    bullet["text"] = f"{bullet['text'].rstrip('.')}, {claim}."
    planted = {"source": item["source"], "tile_id": item["tile_id"], "bullet": 0, "claim": claim}
    with conn:
        conn.execute("UPDATE steps SET answer = ? WHERE preparation_id = ? AND round = 1 AND member = 'writer'",
                     (json.dumps(found, ensure_ascii=False), version["id"]))  # fmt: skip
        conn.execute("UPDATE preparations SET planted = ? WHERE id = ?", (json.dumps(planted), version["id"]))
    version["planted"] = json.dumps(planted)
    return found


def _build(conn: sqlcipher3.Connection, config: Config, preparation: int) -> None:
    if conn.execute("SELECT kind FROM preparations WHERE id = ?", (preparation,)).fetchone()[0] == "pilot":
        return  # never a PDF of a pilot's version: it may hold a made-up claim
    try:
        target, source, note = runs.pdf_source(conn, config, preparation)
        pdf.build_kept(config.data_dir, target, source)
        runs.note_pdf(conn, preparation, note)
    except (pdf.PdfError, privacy.PrivacyError, ResumeError) as error:
        runs.note_pdf(conn, preparation, error)


def _flags(seen: _Seen, flags: list[dict]) -> list[str]:
    """The app's flags on the draft, for the reviewers: numbers the Writer added."""
    labels = {(entry.source, entry.tile.tile_id): entry.label for entry in seen.entries}
    if seen.letter:
        return [f"P{flag['paragraph'] + 1} has numbers in neither the entries nor the ad: {', '.join(flag['numbers'])}"
                for flag in flags]  # fmt: skip
    return [f"{labels[(flag['source'], flag['tile_id'])]}.{flag['bullet'] + 1} has numbers not in its original "
            f"bullets: {', '.join(flag['numbers'])}" for flag in flags]  # fmt: skip


def _pages(seen: _Seen, version: dict, which: str = "version") -> str:
    """The note "The PDF ... must fit on one page" when the version's last built resume (or the one a change builds
    on) is longer, with the one-page setting."""
    if seen.letter or not seen.one_page:
        return ""
    built = pdf.built(pdf.tailored(seen.data_dir, version["job_id"], version[which]))
    return ONE_PAGE.format(built.pages) if built and built.pages > 1 else ""


def _asked(version: dict) -> str:
    """Your words for this version: the change you asked for, and any sent while the council worked."""
    return " ".join([version["feedback"] or "", *(asked["words"] for asked in json.loads(version["asked"]))]).strip()


def _stopped(conn: sqlcipher3.Connection, config: Config, version: dict) -> str | None:
    """Why the version must stop before its next step: the job skipped or closed (unless you proceeded anyway), or
    your entries changed or are not confirmed for your current resume."""
    waived = set(json.loads(version["waived"]))
    if version["job_status"] != "open" and "closed" not in waived:
        return CLOSED
    if version["not_interested"] and "skipped" not in waived:
        return SKIPPED
    try:
        resume = read_resume(config.data_dir).fingerprint
    except ResumeError:
        resume = None
    sources = [found["source"] for found in json.loads(version["sources"])]
    current = runs.current(conn, version["job_id"])
    if runs.outdated(version, current) or writer_sources.unconfirmed(writer_sources.offered(conn, resume), sources):
        return CHANGED
    return None


def _version(conn: sqlcipher3.Connection, preparation: int) -> dict:
    names = ("id", "job_id", "kind", "version", "based_on", "feedback", "asked", "sources", "writer", "council",
             "waived", "round", "status", "draft", "job_status", "not_interested", "base", "base_version",
             "pilot_rounds", "pilot_turns", "claims", "planted")  # fmt: skip
    return dict(zip(names, conn.execute(VERSION, (preparation,)).fetchone(), strict=True))


def end(
    conn: sqlcipher3.Connection, preparation: int, status: str, problem: str | None, agreed: bool | None = None,
    wording: str | None = None, draft: dict | None = None,
) -> None:  # fmt: skip
    with conn:
        conn.execute(ENDED, (
            status, problem, None if agreed is None else int(agreed), wording,
            json.dumps(draft, ensure_ascii=False) if draft else None, _now(), preparation,
        ))  # fmt: skip


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
