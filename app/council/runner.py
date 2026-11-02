"""The council's runner: one pass sends what every version needs now (app/council/flow.py) and saves each answer.

A pass: calls a stopped runner left out are counted and sent again (or given up); finished Anthropic batches are
collected, then deleted there; then every version's next calls are sent, until nothing more can go now. Anthropic at
half price goes in one batch per pass, OpenAI at half price through Flex, up to 4 cloud calls at once; laptop calls go
one at a time, grouped by model, and wait while scoring uses the graphics card. A call is sent only if its largest
possible cost fits your limits. Busy services, expired batch requests and Ollama being off mean waiting (never full
price, never a used try); a refused key pauses that service until you save the key or the settings again."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import sqlcipher3

from app import collect, llm, score
from app.config import OPENAI_HOST, AIRole, Config
from app.council import flow, runs, spending, writer
from app.keychain import KeychainError
from app.llm import anthropic, openai_compatible

LOCK = "prepare-lock.db"  # one runner at a time: the dashboard's or the terminal command's
AT_ONCE = 4  # cloud calls sent together
WAITS = (5, 10, 20, 40)  # minutes a busy call waits, then an hour each time
OLLAMA_AGAIN = timedelta(minutes=10)
ADOPT_AFTER, GIVE_UP = timedelta(minutes=5), timedelta(hours=25)  # a batch whose id a crash lost
REFUSED = "the answer was refused: "
ANTHROPIC = AIRole(service="anthropic", model="batches", key_name="anthropic")  # for the batch pages: address and key
WRITING = "SELECT id FROM preparations WHERE status = 'writing' ORDER BY automatic, created_on, id"
HELD = "SELECT DISTINCT preparation_id FROM steps WHERE status IN ('sent', 'waiting') ORDER BY preparation_id"
STEP = "SELECT id, tries, waits, mode FROM steps WHERE preparation_id = ? AND round = ? AND member = ? AND turn = ?"
INSERT = """INSERT INTO steps (preparation_id, round, member, turn, service, model, mode, status, cost, reserved, day,
    sent_on) VALUES (?, ?, ?, ?, ?, ?, ?, 'sent', ?, ?, ?, ?)"""
AGAIN = """UPDATE steps SET status = 'sent', mode = ?, retry_after = NULL, batch_id = NULL, reserved = ?, day = ?
    WHERE id = ?"""
WAIT = """UPDATE steps SET status = 'waiting', tries = tries + ?, waits = waits + ?, retry_after = ?, batch_id = NULL,
    reserved = 0, error = ? WHERE id = ?"""
DUE = "SELECT 1 FROM steps WHERE id = ? AND status = 'waiting' AND (retry_after IS NULL OR retry_after <= ?)"
LEFT_OUT = "SELECT id, tries, reserved FROM steps WHERE status = 'sent' AND mode != 'batch'"
ORPHANS = "SELECT id, sent_on FROM steps WHERE status = 'sent' AND mode = 'batch' AND batch_id IS NULL"
IN_BATCH = "SELECT id, preparation_id, round, member, turn FROM steps WHERE batch_id = ? AND status = 'sent'"
NOTE = "UPDATE preparations SET problem = ? WHERE id = ? AND status = 'writing'"
LAPTOP = "SELECT writer, council FROM preparations WHERE status = 'writing'"


@dataclass
class State:
    """What a runner remembers between passes: services paused, and why (cleared when you save keys or settings)."""

    paused: dict[str, str] = field(default_factory=dict)
    ollama_after: datetime | None = None  # Ollama was off: laptop calls wait until then


def run(conn: sqlcipher3.Connection, config: Config, state: State, now: datetime | None = None) -> int:
    """One pass (under the runner's lock, held by the caller); returns how many answers were saved."""
    now = now or datetime.now(UTC)
    recover(conn)
    saved = _collect(conn, config, state, now)
    while True:  # until nothing more can go now: each round of sending uses up tries or answers, so it ends
        calls = [call for call in _due(conn, config, now) if _open(call.role, state, now)]
        sent = _reserve(conn, config, calls, now)
        _send_batch(conn, config, state, [found for found in sent if found[2] == "batch"], now)
        moved = _send_direct(conn, config, state, [found for found in sent if found[2] != "batch"], now)
        saved += moved
        if not moved:
            return saved


def recover(conn: sqlcipher3.Connection) -> None:
    """Calls a stopped runner left out (the laptop's, OpenAI's, your own address's): what was set aside counts as
    possibly charged, the try as failed; sent again if a try is left. Batch calls stay: they wait at Anthropic."""
    for step, tries, reserved in conn.execute(LEFT_OUT).fetchall():
        reason = f"Interrupted: the runner stopped while waiting; it may have been charged up to ${reserved:.2f}"
        runs.ended(conn, step, "failed", llm.NOTHING, reserved, Exception(reason))
        if tries < 2:
            with conn:
                conn.execute(WAIT, (1, 0, None, reason[:300], step))


def laptop_work(conn: sqlcipher3.Connection) -> bool:
    """Whether a version being written uses a model on this computer: automatic scoring then waits (owner)."""
    for found in conn.execute(LAPTOP):
        roles = [json.loads(found[0]), *json.loads(found[1] or '{"members": {}}')["members"].values()]
        if any(role.get("service", "ollama") == "ollama" for role in roles):
            return True
    return False


def _due(conn: sqlcipher3.Connection, config: Config, now: datetime) -> list[flow.Call]:
    """Every call that can go now: versions' next calls (yours first, newest last), and waiting ones whose wait is
    over."""
    calls = [call for (preparation,) in conn.execute(WRITING).fetchall()
             for call in flow.advance(conn, config, preparation)]  # fmt: skip
    at = now.isoformat(timespec="seconds")
    for (preparation,) in conn.execute(HELD).fetchall():
        calls += [call for call in flow.pending(conn, config, preparation)
                  if conn.execute(DUE, (_step(conn, call)[0], at)).fetchone()]  # fmt: skip
    return calls


def _open(role: AIRole, state: State, now: datetime) -> bool:
    """Whether the role's service can be used now: not paused, and Ollama not known to be off."""
    if role.service == "ollama":
        return state.ollama_after is None or now >= state.ollama_after
    return _service(role) not in state.paused


def _reserve(conn: sqlcipher3.Connection, config: Config, calls: list[flow.Call], now: datetime) -> list[tuple]:
    """Each call written down as sent, with its largest possible cost set aside, if that fits your limits; laptop
    calls only while no scoring run uses the graphics card. Returns (call, step id, mode, set aside, tries)."""
    scoring = _scoring(config)
    sent, day = [], spending.today()
    for call in calls:
        if call.role.service == "ollama" and scoring:
            continue
        mode = _mode(conn, call)
        largest = spending.largest(call.role, call.system + call.user, call.limit, mode != "direct")
        with conn:  # checked and set aside together, so two calls can't both fit in the same room
            if refusal := spending.refusal(conn, call.role, largest, config.spending, day):
                conn.execute(NOTE, (refusal, call.preparation))
                continue
            step = _step(conn, call)
            if step is None:
                cost = None if largest is None else 0  # NULL: this service's price isn't known, so it is not counted
                step_id, tries = conn.execute(INSERT, (
                    call.preparation, call.round, call.member, call.turn, call.role.service, call.role.model, mode,
                    cost, largest or 0, day, now.isoformat(timespec="seconds"),
                )).lastrowid, 1  # fmt: skip
            else:
                conn.execute(AGAIN, (mode, largest or 0, day, step[0]))
                step_id, tries = step[0], step[1]
        sent.append((call, step_id, mode, largest, tries))
    return sent


def _send_batch(conn: sqlcipher3.Connection, config: Config, state: State, sent: list[tuple], now: datetime) -> None:
    """This pass's Anthropic calls at half price, as one batch; its id is saved the moment it is known."""
    if not sent:
        return
    requests = [anthropic.BatchRequest(f"aa-{step}", call.role, call.system, _text(conn, call, step, tries),
                                       call.shape, call.limit) for call, step, _, _, tries in sent]  # fmt: skip
    try:
        with llm.connected(ANTHROPIC) as (client, key):
            batch = anthropic.send_batch(client, ANTHROPIC, key, requests)
    except (llm.StopScoring, KeychainError) as error:  # never sent: nothing charged
        state.paused["anthropic"] = str(error)
        _wait_all(conn, sent, None, str(error))
        return
    except llm.LLMError as error:
        _wait_all(conn, sent, now + timedelta(minutes=WAITS[0]), str(error))
        return
    with conn:
        conn.execute(
            "INSERT INTO batches (batch_id, sent_on) VALUES (?, ?)", (batch, now.isoformat(timespec="seconds"))
        )
        conn.executemany("UPDATE steps SET batch_id = ? WHERE id = ?", [(batch, step) for _, step, *_ in sent])


def _send_direct(conn: sqlcipher3.Connection, config: Config, state: State, sent: list[tuple], now: datetime) -> int:
    """The other calls: up to AT_ONCE cloud calls together, then the laptop's one at a time, grouped by model. Answers
    are saved here, one by one; returns how many moved their version on (an answer or a used try)."""
    cloud = [found for found in sent if found[0].role.service != "ollama"]
    laptop = sorted(
        (found for found in sent if found[0].role.service == "ollama"), key=lambda found: found[0].role.model
    )
    saved = 0
    with ThreadPoolExecutor(AT_ONCE) as pool:
        futures = [(found, pool.submit(_ask, found, _text(conn, *found[0:2], found[4]))) for found in cloud]
        for found in laptop:
            if not _open(found[0].role, state, now):  # Ollama went off in this pass: the rest wait
                _wait_all(conn, [found], state.ollama_after, state.paused.get("ollama", "Ollama isn't running"))
                continue
            saved += _outcome(conn, config, state, found, _ask(found, _text(conn, *found[0:2], found[4])), now)
        for found, future in futures:
            saved += _outcome(conn, config, state, found, future.result(), now)
    return saved


def _ask(found: tuple, user: str) -> llm.Reply | Exception:
    """One call (in a worker for cloud calls; nothing here touches the database)."""
    call, _, mode, _, tries = found
    try:
        return llm.call(call.role, call.system, user, call.shape, retry=tries > 1, answer_limit=call.limit,
                        timeout=openai_compatible.FLEX_SECONDS if mode == "flex" else call.timeout,
                        half=mode == "flex")  # fmt: skip
    except (llm.LLMError, KeychainError) as error:
        return error


def _outcome(
    conn: sqlcipher3.Connection, config: Config, state: State, found: tuple, result: llm.Reply | Exception,
    now: datetime,
) -> int:  # fmt: skip
    """Saves one call's result: the checked answer, a failed try (sent again once), or a wait. Returns 1 when the
    version moved on (an answer or a used try), 0 for a wait."""
    call, step, mode, largest, tries = found
    half = mode != "direct"
    if isinstance(result, llm.Reply):
        try:
            answer = call.check(result.text)
        except writer.BadAnswer as bad:
            return _failed(conn, found, result.usage, spending.charged(call.role, result.usage, half),
                           Exception(f"{REFUSED}{bad}"), result.text)  # fmt: skip
        runs.ended(conn, step, "done", result.usage, spending.charged(call.role, result.usage, half), None, answer)
        with conn:
            conn.execute(NOTE, (None, call.preparation))
        return 1
    if isinstance(result, llm.Expired):  # not charged, not a try: with the next batch
        _wait_all(conn, [found], None, str(result), waited=1)
        return 0
    if isinstance(result, llm.Busy):  # not charged, not a try: wait a while, longer each time, never full price
        waits = conn.execute("SELECT waits FROM steps WHERE id = ?", (step,)).fetchone()[0]
        _wait_all(conn, [found], now + timedelta(minutes=WAITS[waits] if waits < len(WAITS) else 60), str(result),
                  waited=1)  # fmt: skip
        return 0
    if isinstance(result, llm.TooLong):  # refused, not charged; a retry can't help
        runs.ended(conn, step, "failed", llm.NOTHING, 0, result)
        flow.end(conn, call.preparation, "stopped", runs.TOO_LONG.format(result))
        return 1
    if isinstance(result, (llm.StopScoring, KeychainError)):  # every call to that service would meet it
        if call.role.service == "ollama":
            state.ollama_after, state.paused["ollama"] = now + OLLAMA_AGAIN, str(result)
        else:
            state.paused[_service(call.role)] = str(result)
        _wait_all(conn, [found], state.ollama_after if call.role.service == "ollama" else None, str(result))
        with conn:
            conn.execute(NOTE, (f"Waiting: {result}", call.preparation))
        return 0
    if isinstance(result, llm.TimedOut):  # a paid service may still finish and charge: counted (owner choice M)
        return _failed(conn, found, llm.NOTHING, largest, result)
    return _failed(conn, found, result.usage, spending.charged(call.role, result.usage, half), result)


def _failed(
    conn, found: tuple, usage: llm.Usage, cost: float | None, error: Exception, reply: str | None = None
) -> int:
    """A failed try, kept whole; with a try left the call waits to go again at once, else that member has no valid
    answer (the flow carries on from there)."""
    call, step, _, _, tries = found
    runs.ended(conn, step, "failed", usage, cost, error, reply=reply)
    if tries < 2:
        with conn:
            conn.execute(WAIT, (1, 0, None, str(error)[:300], step))
    return 1


def _collect(conn: sqlcipher3.Connection, config: Config, state: State, now: datetime) -> int:
    """Finished Anthropic batches: each answer saved (a bad one sent again once), then the batch deleted there;
    batches a crash lost are found again first."""
    out = conn.execute("SELECT 1 FROM batches WHERE status = 'sent' UNION SELECT 1 FROM steps WHERE status = 'sent' "
                       "AND mode = 'batch'").fetchone()  # fmt: skip
    if "anthropic" in state.paused or out is None:
        return 0
    saved = 0
    try:
        with llm.connected(ANTHROPIC) as (client, key):
            _adopt(conn, client, key, now)
            for (batch,) in conn.execute("SELECT batch_id FROM batches WHERE status = 'sent'").fetchall():
                if not anthropic.batch_ended(client, ANTHROPIC, key, batch):
                    continue
                results = anthropic.batch_results(client, ANTHROPIC, key, batch)
                for step, preparation, round_, member, turn in conn.execute(IN_BATCH, (batch,)).fetchall():
                    call = next((c for c in flow.pending(conn, config, preparation)
                                 if (c.round, c.member, c.turn) == (round_, member, turn)), None)  # fmt: skip
                    result = results.get(f"aa-{step}", llm.Expired("Anthropic's batch had no result for it"))
                    if call is None:  # its version can't be rebuilt now (e.g. a privacy stop): kept as a failed try
                        usage = result.usage if isinstance(result, llm.Reply) else llm.NOTHING
                        runs.ended(conn, step, "failed", usage, spending.charged(ANTHROPIC, usage, True),
                                   Exception("Its version could not be checked any more"))  # fmt: skip
                        continue
                    found = (call, step, "batch", _reserved(conn, step), _tries(conn, step))
                    saved += _outcome(conn, config, state, found, result, now)
                with conn:
                    conn.execute("UPDATE batches SET status = 'saved', saved_on = ? WHERE batch_id = ?",
                                 (now.isoformat(timespec="seconds"), batch))  # fmt: skip
                anthropic.delete_batch(client, ANTHROPIC, key, batch)
                with conn:
                    conn.execute("UPDATE batches SET status = 'deleted', deleted_on = ? WHERE batch_id = ?",
                                 (now.isoformat(timespec="seconds"), batch))  # fmt: skip
    except (llm.StopScoring, KeychainError) as error:
        state.paused["anthropic"] = str(error)
    except llm.LLMError:
        pass  # busy, expired or garbled: the next pass looks again
    return saved


def _adopt(conn: sqlcipher3.Connection, client, key: str, now: datetime) -> None:
    """Calls written down as sent in a batch whose id a crash lost: found again among your recent batches by their
    names (aa-<step>); after GIVE_UP with none found, they were never sent, and wait to go again (nothing charged)."""
    orphans = {step: datetime.fromisoformat(sent_on) for step, sent_on in conn.execute(ORPHANS).fetchall()}
    old = {step: sent for step, sent in orphans.items() if now - sent >= ADOPT_AFTER}
    if not old:
        return
    known = {batch for (batch,) in conn.execute("SELECT batch_id FROM batches")}
    for batch in anthropic.batches_since(client, ANTHROPIC, key, min(old.values()) - ADOPT_AFTER):
        if batch in known or not anthropic.batch_ended(client, ANTHROPIC, key, batch):
            continue
        names = set(anthropic.batch_results(client, ANTHROPIC, key, batch))
        mine = [step for step in old if f"aa-{step}" in names]
        if mine:
            with conn:
                conn.execute("INSERT INTO batches (batch_id, adopted, sent_on) VALUES (?, 1, ?)",
                             (batch, now.isoformat(timespec="seconds")))  # fmt: skip
                conn.executemany("UPDATE steps SET batch_id = ? WHERE id = ?", [(batch, step) for step in mine])
            old = {step: sent for step, sent in old.items() if step not in mine}
    for step, sent in old.items():
        if now - sent >= GIVE_UP:
            with conn:
                conn.execute(WAIT, (0, 0, None, "Never reached Anthropic: sent again", step))


def _wait_all(conn, sent: list[tuple], after: datetime | None, why: str, waited: int = 0) -> None:
    with conn:
        conn.executemany(WAIT, [(0, waited, after and after.isoformat(timespec="seconds"), why[:300], found[1])
                                for found in sent])  # fmt: skip


def _text(conn: sqlcipher3.Connection, call: flow.Call, step: int, tries: int) -> str:
    """The call's data; a retry after a refused answer also says what was wrong."""
    if tries < 2:
        return call.user
    reason = conn.execute("SELECT reason FROM failed_tries WHERE step_id = ? ORDER BY id DESC LIMIT 1",
                          (step,)).fetchone()  # fmt: skip
    return writer.again(call.user, reason[0][len(REFUSED) :]) if reason and reason[0].startswith(REFUSED) else call.user


def _mode(conn: sqlcipher3.Connection, call: flow.Call) -> str:
    """How a call goes: half price through Anthropic's batches or OpenAI's Flex, unless the job is at normal price;
    the laptop and your own address always directly."""
    normal = conn.execute("SELECT mode FROM preparations WHERE id = ?", (call.preparation,)).fetchone()[0] == "normal"
    if normal or call.role.service == "ollama" or call.role.own:
        return "direct"
    return "batch" if call.role.service == "anthropic" else "flex"


def _service(role: AIRole) -> str:
    if role.service == "anthropic":
        return "anthropic"
    host = urlsplit(role.address).hostname
    return "openai" if host == OPENAI_HOST else f"own {host}"


def _scoring(config: Config) -> bool:
    """Whether a scoring run uses the graphics card now (the laptop's calls then wait)."""
    try:
        with closing(collect.lock(config.data_dir, score.SCORE_LOCK, score.BUSY)):
            return False
    except collect.CollectError:
        return True


def _step(conn: sqlcipher3.Connection, call: flow.Call) -> tuple | None:
    return conn.execute(STEP, (call.preparation, call.round, call.member, call.turn)).fetchone()


def _reserved(conn, step: int) -> float:
    return conn.execute("SELECT reserved FROM steps WHERE id = ?", (step,)).fetchone()[0]


def _tries(conn, step: int) -> int:
    return conn.execute("SELECT tries FROM steps WHERE id = ?", (step,)).fetchone()[0]
