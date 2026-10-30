"""One press of Prepare: the Writer tailors your resume for one job, as a new version with one saved step. The step is
written down before each AI call (with its largest possible cost set aside within your limits) and updated as soon as
the call ends, so a call is never sent twice by itself and its cost is never lost. At most two calls per press: a bad
answer, no answer in time, a busy service or a cut-off answer gets one retry. A version ends "done", "by_hand" (check
by hand: Retry starts a new version) or "stopped" (nothing more was sent: fix the cause, then press Prepare again).
Messages name the cause and the fix, never your text, the job ad or the answer."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import sqlcipher3

from app import llm, questionnaire
from app.config import AIRole, Config
from app.council import spending, writer
from app.documents import latex, pdf, privacy, tile_sets, uploads, writer_sources
from app.keychain import KeychainError
from app.resume import ResumeError, read_latex

KIND = "resume"  # the cover letter is its own kind
LABELS = {"writing": "writing", "done": "done", "by_hand": "check by hand", "stopped": "stopped"}  # as pages say them
JOB = "SELECT title, company, locations, description FROM jobs WHERE id = ?"
NEWEST = """SELECT id, version, status, problem, sources, draft, flags, feedback, created_on FROM preparations
    WHERE job_id = ? AND kind = ? {} ORDER BY version DESC LIMIT 1"""
LATEST, LAST_DONE = NEWEST.format(""), NEWEST.format("AND status = 'done'")
START = """INSERT INTO preparations (job_id, kind, version, based_on, feedback, sources, writer, status, created_on)
    VALUES (?, ?, (SELECT coalesce(max(version), 0) + 1 FROM preparations WHERE job_id = ? AND kind = ?), ?, ?, ?, ?,
    'writing', ?)"""
FINISH = "UPDATE preparations SET status = ?, problem = ?, draft = ?, flags = ?, finished_on = ? WHERE id = ?"
FIRST_SEND = """INSERT INTO steps (preparation_id, service, model, status, cost, reserved, day, sent_on)
    VALUES (?, ?, ?, 'sent', ?, ?, ?, ?)"""
SEND_AGAIN = "UPDATE steps SET status = 'sent', tries = tries + 1, reserved = ? WHERE id = ?"
# NULL + a cost stays NULL: a step whose price isn't known stays "not counted"
ENDED = """UPDATE steps SET status = ?, input_tokens = input_tokens + ?, cached_tokens = cached_tokens + ?,
    output_tokens = output_tokens + ?, cost = cost + ?, reserved = 0, answer = ?, error = ?, done_on = ? WHERE id = ?"""
LEFT_WAITING = "SELECT id, preparation_id, reserved FROM steps WHERE status = 'sent'"
INTERRUPT = """UPDATE steps SET status = 'interrupted', cost = cost + reserved, reserved = 0, error = ?, done_on = ?
    WHERE id = ?"""
LEFT_WRITING = "SELECT id FROM preparations WHERE status = 'writing'"
VERSION = "SELECT job_id, version, sources, draft FROM preparations WHERE id = ?"
NOTE = "UPDATE preparations SET problem = ? WHERE id = ?"
VERSIONS = """SELECT p.id, p.version, p.status, p.created_on, p.feedback,
    coalesce(sum(coalesce(s.cost, 0) + s.reserved), 0), coalesce(max(s.id IS NOT NULL AND s.cost IS NULL), 0)
    FROM preparations p LEFT JOIN steps s ON s.preparation_id = p.id WHERE p.job_id = ? AND p.kind = ?
    GROUP BY p.id ORDER BY p.version DESC"""
ERROR = 300  # characters of a failed try's reason kept with its step
CLOSED = "Interrupted: the dashboard closed while the Writer was working"
NO_BASE = "There is no finished version to change yet: press Prepare first."
CHANGED = "Your entries changed since version {}: press Prepare for a fresh version first, then ask for changes."


def latest(conn: sqlcipher3.Connection, job_id: int, kind: str = KIND, query: str = LATEST) -> dict | None:
    """The job's newest version of its resume (or cover letter), or None."""
    rows = conn.execute(query, (job_id, kind))
    row = rows.fetchone()
    return dict(zip([column[0] for column in rows.description], row, strict=True)) if row else None


def last_done(conn: sqlcipher3.Connection, job_id: int, kind: str = KIND) -> dict | None:
    """The job's newest finished version: what Request changes builds on."""
    return latest(conn, job_id, kind, LAST_DONE)


def versions(conn: sqlcipher3.Connection, job_id: int, kind: str = KIND) -> list[dict]:
    """Every version of the job's resume, newest first, with what it cost (and whether some was not counted)."""
    names = ("id", "version", "status", "created_on", "feedback", "cost", "uncounted")
    return [dict(zip(names, row, strict=True)) for row in conn.execute(VERSIONS, (job_id, kind))]


def writable(conn: sqlcipher3.Connection, job_id: int, version: dict | None) -> bool:
    """Whether Prepare writes a new version: none yet, the last one stopped or needs checking by hand (Retry), or it
    was made from older entries. Never while one is being written, nor twice from the same entries."""
    if version is None or version["status"] in ("stopped", "by_hand"):
        return True
    return version["status"] == "done" and outdated(conn, job_id, version)


def outdated(conn: sqlcipher3.Connection, job_id: int, version: dict) -> bool:
    """Whether a version was made from other entries than the job's sources have now (or from other sources)."""
    sources = writer_sources.of_job(conn, job_id)[0]
    return json.loads(version["sources"]) != [{"source": source, "set": _set(conn, source)} for source in sources]


def prepare(conn: sqlcipher3.Connection, config: Config, job_id: int, feedback: str = "") -> int:
    """One press of Prepare for one job's resume, or of Request changes with your words (a new version built on the
    newest finished one); returns the new version's id. Needs a Writer in the settings."""
    role, sources = config.ai.writer, writer_sources.of_job(conn, job_id)[0]
    sets = [{"source": source, "set": _set(conn, source)} for source in sources]
    base = last_done(conn, job_id) if feedback else None
    with conn:
        preparation = conn.execute(START, (
            job_id, KIND, job_id, KIND, base and base["id"], feedback or None, json.dumps(sets),
            role.model_dump_json(), _now(),
        )).lastrowid  # fmt: skip
    try:
        if feedback and (base is None or outdated(conn, job_id, base)):
            status, problem, result = "stopped", NO_BASE if base is None else CHANGED.format(base["version"]), None
        else:
            status, problem, result = _write(conn, config, role, job_id, sources, preparation, base, feedback)
    except Exception as error:  # never left "writing": the version can be retried
        _finish(conn, preparation, "by_hand", f"Something went wrong ({type(error).__name__}): press Retry.")
        raise
    _finish(conn, preparation, status, problem, result)
    if status == "done":
        try:
            target, source = pdf_source(conn, config, preparation)
            pdf.build_kept(config.data_dir, target, source)
            note_pdf(conn, preparation, None)
        except (pdf.PdfError, privacy.PrivacyError, ResumeError) as error:
            note_pdf(conn, preparation, error)
    return preparation


def pdf_source(conn: sqlcipher3.Connection, config: Config, preparation: int) -> tuple[Path, str]:
    """Where a version's tailored PDF goes, and its LaTeX: your original fixed facts, the Writer's bullets and skills
    with your details put back (on this computer only), and the Questionnaire's contact header."""
    job_id, number, sources, draft = conn.execute(VERSION, (preparation,)).fetchone()
    found = originals(conn, sources)
    shield = shield_for(conn, config, found)
    by_id = {(entry.source, entry.tile.tile_id): entry.tile for entry in found}
    tiles = [
        _tailored(by_id[(chosen["source"], chosen["tile_id"])], chosen, shield)
        for chosen in json.loads(draft)["entries"]
    ]
    source = latex.resume(questionnaire.load(conn)[0]["contact"], tiles)
    return pdf.tailored(config.data_dir, job_id, number), source


def note_pdf(conn: sqlcipher3.Connection, preparation: int, error: Exception | None) -> None:
    """A failed build keeps the draft and says why (the kind of error and its line, never your text)."""
    with conn:
        conn.execute(NOTE, (f"PDF could not be built: {error}" if error else None, preparation))


def originals(conn: sqlcipher3.Connection, sources: str) -> list[writer.Entry]:
    """The entries a version was made from (its recorded sets, as JSON), in the order of your resume."""
    return writer.in_order([(found["source"], tile_sets.tiles(conn, found["set"])) for found in json.loads(sources)])


def shield_for(conn: sqlcipher3.Connection, config: Config, entries: list[writer.Entry]) -> privacy.Shield:
    """The privacy filter for these entries and their sources, as the Writer's text is hidden with."""
    sources = list(dict.fromkeys(entry.source for entry in entries))
    latex_texts = [read_latex(config.data_dir) if source == writer_sources.PROFILE else uploads.latex(conn, source)
                   for source in sources]  # fmt: skip
    return privacy.shield(questionnaire.load(conn)[0]["contact"], latex_texts, [entry.tile for entry in entries])


def _tailored(tile, chosen: dict, shield: privacy.Shield):
    """One tile as the Writer chose it: its fixed facts unchanged, its bullets and skills the draft's."""
    skills = {"skills": [shield.restore(skill) for skill in chosen["skills"]]} if tile.kind == "skills" else {}
    return replace(tile, facts=tile.facts | skills, bullets=tuple(shield.restore(b["text"]) for b in chosen["bullets"]))


def recover(conn: sqlcipher3.Connection) -> None:
    """Calls a closed dashboard left waiting: marked interrupted, never sent again by themselves, their set-aside
    amount counted as possibly charged; their versions need checking by hand. Only while holding the Writer's lock,
    so no Writer is running anywhere."""
    with conn:
        charged = {}
        for step, preparation, reserved in conn.execute(LEFT_WAITING).fetchall():
            conn.execute(INTERRUPT, (CLOSED, _now(), step))
            charged[preparation] = reserved
        for (preparation,) in conn.execute(LEFT_WRITING).fetchall():
            reserved = charged.get(preparation, 0)
            maybe = f"; it may have been charged up to ${reserved:.2f}" if reserved else ""
            conn.execute(FINISH, ("by_hand", f"{CLOSED}{maybe}. Press Retry.", None, "[]", _now(), preparation))


def _write(
    conn: sqlcipher3.Connection, config: Config, role: AIRole, job_id: int, sources: list, preparation: int,
    base: dict | None, feedback: str,
) -> tuple[str, str | None, writer.Checked | None]:  # fmt: skip
    title, company, locations, description = conn.execute(JOB, (job_id,)).fetchone()
    found = writer.entries(conn, sources)
    try:
        shield = shield_for(conn, config, found)
        previous = json.loads(base["draft"]) if base else None
        system, user = writer.request(title, company, ", ".join(json.loads(locations)), description or "", found,
                                      shield, previous, feedback + _too_long(config, job_id, base))  # fmt: skip
    except (privacy.PrivacyError, ResumeError) as error:
        return "stopped", str(error), None
    except privacy.PrivacyStop as stop:
        return "stopped", f"Privacy check stopped this: {stop}. Change that, then try again.", None
    step, problems, text = None, [], user
    for retry in (False, True):
        largest = spending.largest(role, system + text, writer.ANSWER_LIMIT)
        with conn:  # checked and set aside together, so two calls can't both fit in the same room
            if refusal := spending.refusal(conn, role, largest, config.spending, spending.today()):
                return "stopped", "; ".join([*problems, refusal]), None
            step = _send(conn, preparation, role, largest, step)
        try:
            reply = llm.call(role, system, text, writer.SHAPE, retry=retry, answer_limit=writer.ANSWER_LIMIT,
                             timeout=writer.TIMEOUT_SECONDS)  # fmt: skip
        except llm.TimedOut as error:  # a paid service may still finish and charge: counted (owner choice M)
            _ended(conn, step, "failed", llm.NOTHING, largest, error)  # free ($0) for the local model
            problems.append(str(error))
            continue
        except (llm.StopScoring, llm.TooLong, KeychainError) as error:  # refused, never charged; a retry can't help
            _ended(conn, step, "failed", llm.NOTHING, 0, error)
            return "stopped", _stopped(error), None
        except llm.LLMError as error:  # busy, cut off, declined, not JSON: one failed try
            _ended(conn, step, "failed", error.usage, spending.charged(role, error.usage), error)
            problems.append(str(error))
            continue
        try:
            result = writer.checked(reply.text, found, shield)
        except writer.BadAnswer as bad:
            _ended(conn, step, "failed", reply.usage, spending.charged(role, reply.usage), bad)
            problems.append(f"the answer was refused: {bad}")
            text = writer.again(user, str(bad))
            continue
        _ended(conn, step, "done", reply.usage, spending.charged(role, reply.usage), None, result.draft)
        return "done", None, result
    return "by_hand", f"Writer failed twice ({'; then '.join(problems)}): check by hand, or press Retry.", None


def _send(conn: sqlcipher3.Connection, preparation: int, role: AIRole, largest: float | None, step: int | None) -> int:
    """Write the call down before it is sent, with its largest possible cost set aside (0 when it is free or not
    counted). The first try adds the step; the retry sends the same step again."""
    if step is not None:
        conn.execute(SEND_AGAIN, (largest or 0, step))
        return step
    cost = None if largest is None else 0  # NULL: this service's price isn't known, so it is not counted
    return conn.execute(
        FIRST_SEND, (preparation, role.service, role.model, cost, largest or 0, spending.today(), _now())
    ).lastrowid


def _ended(
    conn: sqlcipher3.Connection, step: int, status: str, usage: llm.Usage, cost: float | None,
    error: Exception | None, answer: dict | None = None,
) -> None:  # fmt: skip
    """The call ended: its tokens and cost replace what was set aside."""
    with conn:
        conn.execute(ENDED, (
            status, *usage, cost, json.dumps(answer, ensure_ascii=False) if answer else None,
            str(error)[:ERROR] if error else None, _now(), step,
        ))  # fmt: skip


def _too_long(config: Config, job_id: int, base: dict | None) -> str:
    """For a change to a version whose PDF went over one page (with the one-page setting on): a note saying so."""
    built = pdf.built(pdf.tailored(config.data_dir, job_id, base["version"])) if base else None
    if built is None or built.pages <= 1 or not config.documents.one_page:
        return ""
    return f"\n(The previous version was {built.pages} pages: it must fit on one page.)"


def _stopped(error: Exception) -> str:
    if isinstance(error, llm.TooLong):
        return f"Too long for the model ({error}): raise the working size on the Writer settings page."
    return str(error)  # it names the cause and the fix (a key, the model's name, Ollama, the keychain)


def _finish(
    conn: sqlcipher3.Connection, preparation: int, status: str, problem: str | None,
    result: writer.Checked | None = None,
) -> None:  # fmt: skip
    draft, flags = (json.dumps(result.draft, ensure_ascii=False), json.dumps(result.flags)) if result else (None, "[]")
    with conn:
        conn.execute(FINISH, (status, problem, draft, flags, _now(), preparation))


def _set(conn: sqlcipher3.Connection, source) -> int | None:
    confirmed = tile_sets.latest(conn, None if source == writer_sources.PROFILE else source)
    return confirmed.set_id if confirmed else None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
