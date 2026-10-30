"""One press of Prepare: the Writer tailors your resume for one job, as a new version with one saved step. The step is
written down before each AI call (with its largest possible cost set aside within your limits) and updated as soon as
the call ends, so a call is never sent twice by itself and its cost is never lost. At most two calls per press: a bad
answer, no answer in time, a busy service or a cut-off answer gets one retry. A version ends "done", "by_hand" (check
by hand: Retry starts a new version) or "stopped" (nothing more was sent: fix the cause, then press Prepare again).
Messages name the cause and the fix, never your text, the job ad or the answer."""

import json
from datetime import UTC, datetime

import sqlcipher3

from app import llm, questionnaire
from app.config import AIRole, Config
from app.council import spending, writer
from app.documents import privacy, tile_sets, uploads, writer_sources
from app.keychain import KeychainError
from app.resume import ResumeError, read_latex

KIND = "resume"  # the cover letter (piece 9) is its own kind
JOB = "SELECT title, company, locations, description FROM jobs WHERE id = ?"
LATEST = """SELECT id, version, status, problem, sources, draft, flags, created_on FROM preparations
    WHERE job_id = ? AND kind = ? ORDER BY version DESC LIMIT 1"""
START = """INSERT INTO preparations (job_id, kind, version, sources, writer, status, created_on) VALUES (?, ?,
    (SELECT coalesce(max(version), 0) + 1 FROM preparations WHERE job_id = ? AND kind = ?), ?, ?, 'writing', ?)"""
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
ERROR = 300  # characters of a failed try's reason kept with its step
CLOSED = "Interrupted: the dashboard closed while the Writer was working"


def latest(conn: sqlcipher3.Connection, job_id: int, kind: str = KIND) -> dict | None:
    """The job's newest version of its resume (or cover letter), or None."""
    rows = conn.execute(LATEST, (job_id, kind))
    row = rows.fetchone()
    return dict(zip([column[0] for column in rows.description], row, strict=True)) if row else None


def outdated(conn: sqlcipher3.Connection, job_id: int, version: dict) -> bool:
    """Whether a version was made from other entries than the job's sources have now (or from other sources)."""
    sources = writer_sources.of_job(conn, job_id)[0]
    return json.loads(version["sources"]) != [{"source": source, "set": _set(conn, source)} for source in sources]


def prepare(conn: sqlcipher3.Connection, config: Config, job_id: int) -> int:
    """One press of Prepare for one job's resume; returns the new version's id. Needs a Writer in the settings."""
    role, sources = config.ai.writer, writer_sources.of_job(conn, job_id)[0]
    sets = [{"source": source, "set": _set(conn, source)} for source in sources]
    with conn:
        preparation = conn.execute(START, (
            job_id, KIND, job_id, KIND, json.dumps(sets), role.model_dump_json(), _now()
        )).lastrowid  # fmt: skip
    try:
        status, problem, result = _write(conn, config, role, job_id, sources, preparation)
    except Exception as error:  # never left "writing": the version can be retried
        _finish(conn, preparation, "by_hand", f"Something went wrong ({type(error).__name__}): press Retry.")
        raise
    _finish(conn, preparation, status, problem, result)
    return preparation


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
    conn: sqlcipher3.Connection, config: Config, role: AIRole, job_id: int, sources: list, preparation: int
) -> tuple[str, str | None, writer.Checked | None]:
    title, company, locations, description = conn.execute(JOB, (job_id,)).fetchone()
    found = writer.entries(conn, sources)
    try:
        latex = [read_latex(config.data_dir) if source == writer_sources.PROFILE else uploads.latex(conn, source)
                 for source in sources]  # fmt: skip
        shield = privacy.shield(questionnaire.load(conn)[0]["contact"], latex, [entry.tile for entry in found])
        system, user = writer.request(
            title, company, ", ".join(json.loads(locations)), description or "", found, shield
        )
    except (privacy.PrivacyError, ResumeError) as error:
        return "stopped", str(error), None
    except privacy.PrivacyStop as stop:
        return "stopped", f"Privacy check stopped this: {stop}. Edit that entry, then press Prepare again.", None
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
