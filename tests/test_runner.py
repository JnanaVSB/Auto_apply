"""The council's runner against fake services: the laptop, OpenAI (Flex) and Anthropic (batches) are all played here,
with a made-up person, job and answers; nothing here reaches a real AI."""

from concurrent.futures import Future
from contextlib import closing
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from helpers import add_job, one
from test_flow import APPROVE, GOOD, made_up_job
from test_runs import LOCAL

from app import collect, llm, score
from app.config import AIRole, AISettings, Config, CouncilSettings, SpendingSettings
from app.council import flow, reviewers, runner, writer
from app.keychain import SERVICE
from app.llm.anthropic import send_batch

QWEN = AIRole(model="qwen3.6:35b-a3b")
OPUS = AIRole(service="anthropic", model="claude-opus-5-5", key_name="anthropic")
SOL = AIRole(service="openai-compatible", model="gpt-6.1-sol", address="https://api.openai.com/v1", key_name="openai")
NOW = datetime(2026, 11, 1, 23, tzinfo=UTC)
USED = llm.Usage(1000, 0, 200)
MEMBERS = ("truth", "hiring", "editor", "ats", "domain")
STATE = "SELECT status, round, agreed, problem FROM preparations"
STEPS = "SELECT member, mode, status, tries, waits, retry_after IS NOT NULL FROM steps ORDER BY id"


@pytest.fixture
def job(conn, data_dir):
    return made_up_job(conn, data_dir)


@pytest.fixture(autouse=True)
def built(monkeypatch):
    monkeypatch.setattr(flow.pdf, "build_kept", lambda data_dir, target, source: None)  # never Tectonic here


def settings(data_dir, writer_=LOCAL, reviewer=LOCAL, daily=15, **roles):
    ai = dict.fromkeys(MEMBERS, reviewer) | roles
    return Config(data_dir=data_dir, ai=AISettings(writer=writer_, **ai), council=CouncilSettings(turns=0),
                  spending=SpendingSettings(daily=daily))  # fmt: skip


def member(system: str) -> str:
    """Which member a call is for, from its instructions."""
    if system.startswith(writer.SKILL_FILE.read_text(encoding="utf-8").strip()[:40]):
        return "writer"
    return next(m for m in MEMBERS if system.startswith(reviewers.instructions(m, reviewers.SKILLS)[:40]))


@pytest.fixture
def models(monkeypatch):
    """The fake models: each call recorded; answers by member (a text, a Reply or an error), else a good one."""
    calls, scripted = [], {}

    def call(role, system, user, shape, *, retry, answer_limit, timeout, half):
        who = member(system)
        calls.append({"member": who, "model": role.model, "user": user, "retry": retry, "timeout": timeout,
                      "half": half})  # fmt: skip
        reply = scripted[who].pop(0) if scripted.get(who) else (GOOD if who == "writer" else APPROVE)
        if isinstance(reply, BaseException):
            raise reply
        return reply if isinstance(reply, llm.Reply) else llm.Reply(reply, USED)

    monkeypatch.setattr(runner.llm, "call", call)
    monkeypatch.setattr(runner, "_scoring", lambda config: False)
    return calls, scripted


def run(conn, config, state=None, now=NOW):
    state = state or runner.State()
    runner.run(conn, config, state, now)
    return state


def test_a_laptop_council_runs_to_agreement_in_one_pass_grouped_by_model(conn, data_dir, job, models):
    calls, _ = models
    config = settings(data_dir, reviewer=QWEN, ats=LOCAL)

    flow.start(conn, config, job)
    run(conn, config)
    assert one(conn, STATE)[:3] == ("done", 1, 1)
    assert [c["member"] for c in calls] == ["writer", "ats", "truth", "hiring", "editor", "domain"]  # gemma, then Qwen
    assert {row[1:4] for row in conn.execute(STEPS)} == {("direct", "done", 1)}


def test_a_bad_answer_is_sent_again_once_saying_what_was_wrong_then_no_valid_answer(conn, data_dir, job, models):
    calls, scripted = models
    config = settings(data_dir)

    flow.start(conn, config, job)
    scripted["writer"] = ["not json"]
    scripted["truth"] = ["no", "still no"]
    run(conn, config)
    writer_calls = [c for c in calls if c["member"] == "writer"]
    assert (
        writer_calls[1]["retry"]
        and "Your previous answer was refused: the answer is not JSON" in writer_calls[1]["user"]
    )
    rows = {row[0]: row[1:] for row in conn.execute(STEPS.replace("ORDER BY", "WHERE round = 1 ORDER BY"))}
    assert rows["writer"] == ("direct", "done", 2, 0, 0) and rows["truth"] == ("direct", "failed", 2, 0, 0)
    assert one(conn, "SELECT count(*) FROM failed_tries")[0] == 3
    assert one(conn, STATE)[:3] == ("done", 2, 1)  # round 1 couldn't agree on a broken answer; round 2 did


def test_a_busy_service_waits_longer_each_time_without_using_a_try(conn, data_dir, job, models, fake_keychain):
    calls, scripted = models
    fake_keychain.store[(SERVICE, "openai")] = "fake-key"
    config = settings(data_dir, writer_=SOL)

    flow.start(conn, config, job)
    scripted["writer"] = [llm.Busy("OpenAI is busy (status 429)"), llm.Busy("busy again")]
    state = run(conn, config)
    assert one(conn, STEPS) == ("writer", "flex", "waiting", 1, 1, 1)
    assert calls[0]["half"] and calls[0]["timeout"] == 900  # Flex: half price, a longer wait
    run(conn, config, state, NOW + timedelta(minutes=4))
    assert len(calls) == 1  # not before its time
    run(conn, config, state, NOW + timedelta(minutes=5))
    assert one(conn, STEPS)[2:5] == ("waiting", 1, 2)
    after = one(conn, "SELECT retry_after FROM steps")[0]
    assert after == (NOW + timedelta(minutes=15)).isoformat(timespec="seconds")  # then 10 minutes more
    run(conn, config, state, NOW + timedelta(minutes=15))
    assert one(conn, STATE)[:2] == ("done", 1) and one(conn, "SELECT count(*) FROM failed_tries")[0] == 0


def test_no_answer_in_time_counts_what_was_set_aside_and_tries_again(conn, data_dir, job, models, fake_keychain):
    _, scripted = models
    fake_keychain.store[(SERVICE, "openai")] = "fake-key"
    config = settings(data_dir, writer_=SOL)

    flow.start(conn, config, job)
    scripted["writer"] = [llm.TimedOut("no answer within 900 seconds")] * 2
    run(conn, config)
    cost, tries, status = one(conn, "SELECT cost, tries, status FROM steps WHERE member = 'writer'")
    assert (tries, status) == (2, "failed") and cost > 0.02  # both reservations: it may have been charged


def test_ollama_off_waits_ten_minutes_and_says_why(conn, data_dir, job, models):
    calls, scripted = models
    config = settings(data_dir)
    flow.start(conn, config, job)
    scripted["truth"] = [llm.StopScoring("Ollama isn't running: start it with `ollama serve`.")]
    state = run(conn, config)
    assert [c["member"] for c in calls] == ["writer", "truth"]  # the other laptop calls wait too
    assert "Waiting: Ollama isn't running" in one(conn, STATE)[3]
    run(conn, config, state, NOW + timedelta(minutes=9))
    assert len(calls) == 2
    scripted.update({m: [llm.Busy("busy")] for m in MEMBERS[1:]})
    run(conn, config, state, NOW + timedelta(minutes=10))
    assert one(conn, STATE)[0] == "writing" and one(conn, STATE)[3] is None  # the note goes with the next answer


def test_a_refused_key_pauses_that_service_only(conn, data_dir, job, models, fake_keychain):
    calls, scripted = models
    fake_keychain.store[(SERVICE, "openai")] = "fake-key"
    config = settings(data_dir, truth=SOL)

    flow.start(conn, config, job)
    scripted["truth"] = [llm.StopScoring("The AI service refused the key (status 401): check the key.")]
    state = run(conn, config)
    assert state.paused == {"openai": "The AI service refused the key (status 401): check the key."}
    assert {row[0]: row[2] for row in conn.execute(STEPS)} == {m: "done" for m in ("writer", *MEMBERS[1:])} | {
        "truth": "waiting"
    }
    run(conn, config, state, NOW + timedelta(hours=5))
    assert sum(c["member"] == "truth" for c in calls) == 1  # never tried again until you save the key


def test_a_call_that_does_not_fit_the_limits_pauses_its_job(conn, data_dir, job, models, fake_keychain):
    calls, _ = models
    fake_keychain.store[(SERVICE, "anthropic")] = "fake-key"
    config = settings(data_dir, writer_=OPUS, daily=1)

    flow.start(conn, config, job, mode="normal")
    with conn:
        conn.execute("INSERT INTO preparations (job_id, kind, version, sources, writer, status, draft, created_on) "
                     "VALUES (?, 'cover_letter', 1, '[]', '{}', 'done', '{}', 'now')", (job,))  # fmt: skip
        conn.execute("INSERT INTO steps (preparation_id, service, model, status, cost, day, sent_on) "
                     "VALUES (2, 'anthropic', 'm', 'done', 0.99, ?, 'now')", (runner.spending.today(),))  # fmt: skip
    run(conn, config)
    assert calls == [] and one(conn, STATE + " WHERE id = 1")[3].startswith("Paused: today's spending limit")


def test_normal_price_sends_anthropic_directly(conn, data_dir, job, models, fake_keychain):
    calls, _ = models
    fake_keychain.store[(SERVICE, "anthropic")] = "fake-key"
    config = settings(data_dir, writer_=OPUS)

    flow.start(conn, config, job, mode="normal")
    run(conn, config)
    assert calls[0]["member"] == "writer" and not calls[0]["half"] and one(conn, STEPS)[1] == "direct"


@pytest.fixture
def batches(monkeypatch, fake_keychain):
    """A fake Anthropic for batches: sent ones recorded, results given by the test."""
    fake_keychain.store[(SERVICE, "anthropic")] = "fake-key"
    found = {"sent": [], "ended": set(), "results": {}, "deleted": [], "listed": []}
    a = runner.anthropic

    def send(client, role, key, requests):
        found["sent"].append([request.name for request in requests])
        return f"b{len(found['sent'])}"

    monkeypatch.setattr(a, "send_batch", send)
    monkeypatch.setattr(a, "batch_ended", lambda client, role, key, batch: batch in found["ended"])
    monkeypatch.setattr(a, "batch_results", lambda client, role, key, batch: found["results"][batch])
    monkeypatch.setattr(a, "delete_batch", lambda client, role, key, batch: found["deleted"].append(batch))
    monkeypatch.setattr(a, "batches_since", lambda client, role, key, since: found["listed"])
    return found


def test_anthropic_calls_go_in_a_batch_collected_then_deleted(conn, data_dir, job, models, batches, monkeypatch):
    config = settings(data_dir, writer_=OPUS)
    asked = []
    largest = runner.spending.largest
    monkeypatch.setattr(runner.spending, "largest", lambda *args: asked.append(args[3]) or largest(*args))

    flow.start(conn, config, job)
    run(conn, config)
    assert batches["sent"] == [["aa-1"]] and one(conn, "SELECT batch_id, mode, status FROM steps") == (
        "b1", "batch", "sent",
    )  # fmt: skip
    assert one(conn, "SELECT status, adopted FROM batches") == ("sent", 0) and asked == [True]  # set aside at half
    run(conn, config)  # still working at Anthropic: nothing more
    assert len(batches["sent"]) == 1
    batches["ended"].add("b1")
    batches["results"]["b1"] = {"aa-1": llm.Reply(GOOD, USED)}
    run(conn, config)
    assert batches["deleted"] == ["b1"] and one(conn, "SELECT status FROM batches")[0] == "deleted"
    cost = one(conn, "SELECT cost FROM steps WHERE id = 1")[0]
    assert cost == pytest.approx((1000 * 2 + 200 * 10) / 1e6)  # half price
    assert one(conn, STATE)[:3] == ("done", 1, 1)  # then the laptop reviewers, in the same pass


def test_an_expired_batch_request_goes_in_the_next_batch_uncharged(conn, data_dir, job, models, batches):
    config = settings(data_dir, writer_=OPUS)

    flow.start(conn, config, job)
    run(conn, config)
    batches["ended"].add("b1")
    batches["results"]["b1"] = {"aa-1": llm.Expired("not within 24 hours")}
    run(conn, config)
    assert batches["sent"] == [["aa-1"], ["aa-1"]] and one(conn, "SELECT cost, tries, batch_id FROM steps") == (
        0, 1, "b2",
    )  # fmt: skip


def test_a_batch_a_crash_lost_is_found_again_or_sent_again_after_a_day(conn, data_dir, job, models, batches):
    config = settings(data_dir, writer_=OPUS)

    flow.start(conn, config, job)
    monkey_sent = runner.anthropic.send_batch
    runner.anthropic.send_batch = lambda *args: (_ for _ in ()).throw(KeyboardInterrupt())  # the crash
    with pytest.raises(KeyboardInterrupt):
        run(conn, config)
    runner.anthropic.send_batch = monkey_sent
    assert one(conn, "SELECT status, batch_id FROM steps") == ("sent", None)
    batches["listed"] = ["b7"]
    batches["ended"].add("b7")
    batches["results"]["b7"] = {"aa-1": llm.Reply(GOOD, USED)}
    run(conn, config, now=NOW + timedelta(minutes=10))
    assert one(conn, "SELECT batch_id, adopted FROM batches") == ("b7", 1) and one(conn, STATE)[0] == "done"


def test_an_orphan_no_batch_holds_is_sent_again_after_a_day(conn, data_dir, job, models, batches):
    config = settings(data_dir, writer_=OPUS)

    flow.start(conn, config, job)
    with conn:
        conn.execute("INSERT INTO steps (preparation_id, service, model, mode, status, reserved, day, sent_on) "
                     "VALUES (1, 'anthropic', 'claude-opus-5-5', 'batch', 'sent', 0.3, 'd', ?)",
                     (NOW.isoformat(timespec="seconds"),))  # fmt: skip
    run(conn, config, now=NOW + timedelta(hours=1))
    assert one(conn, "SELECT status FROM steps")[0] == "sent" and batches["sent"] == []
    run(conn, config, now=NOW + timedelta(hours=26))
    assert batches["sent"] == [["aa-1"]] and one(conn, "SELECT reserved > 0 FROM steps")[0]  # set aside again


def test_a_batch_whose_answer_was_lost_is_found_again_never_sent_twice(conn, data_dir, job, models, batches,
                                                                       monkeypatch):  # fmt: skip
    config = settings(data_dir, writer_=OPUS)
    fake, replies = runner.anthropic.send_batch, [llm.Busy("Anthropic is busy"), llm.TimedOut("the connection broke")]

    def send(*args):
        reply = replies.pop(0) if replies else None
        if not isinstance(reply, llm.Busy):
            fake(*args)  # made at Anthropic, and then its answer lost (or not)
        if reply:
            raise reply
        return f"b{len(batches['sent'])}"

    monkeypatch.setattr(runner.anthropic, "send_batch", send)
    flow.start(conn, config, job)
    run(conn, config)  # busy: nothing made, sent again later, not a try
    assert one(conn, "SELECT status, tries, retry_after IS NOT NULL FROM steps") == ("waiting", 1, 1)
    later = NOW + timedelta(days=2)
    run(conn, config, now=later)
    assert batches["sent"] == [["aa-1"]] and one(conn, "SELECT status, batch_id FROM steps") == ("sent", None)
    batches["listed"] = ["b1"]
    run(conn, config, now=later + timedelta(minutes=6))  # found, but not ended: waited for, never sent again
    assert batches["sent"] == [["aa-1"]] and one(conn, "SELECT status FROM steps")[0] == "sent"
    batches["ended"].add("b1")
    batches["results"]["b1"] = {"aa-1": llm.Reply(GOOD, USED)}
    run(conn, config, now=later + timedelta(minutes=12))
    assert one(conn, "SELECT batch_id, adopted, status FROM batches") == ("b1", 1, "deleted")
    assert batches["sent"] == [["aa-1"]] and one(conn, STATE)[0] == "done"


def test_a_gateway_error_after_the_batch_was_made_is_found_again_never_sent_twice(conn, data_dir, job, models, batches,
                                                                                monkeypatch):  # fmt: skip
    config = settings(data_dir, writer_=OPUS)

    def send(client, role, key, requests):  # Anthropic makes the batch, then a gateway in between answers 504
        batches["sent"].append([request.name for request in requests])
        with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(504))) as gateway:
            return send_batch(gateway, role, key, requests)

    monkeypatch.setattr(runner.anthropic, "send_batch", send)
    flow.start(conn, config, job)
    run(conn, config)
    assert one(conn, "SELECT status, batch_id, tries FROM steps") == ("sent", None, 1)  # it may exist: looked for
    batches["listed"] = ["b1"]
    batches["ended"].add("b1")
    batches["results"]["b1"] = {"aa-1": llm.Reply(GOOD, USED)}
    run(conn, config, now=NOW + timedelta(minutes=6))
    assert batches["sent"] == [["aa-1"]] and one(conn, "SELECT batch_id, adopted FROM batches") == ("b1", 1)
    assert one(conn, STATE)[0] == "done"


def test_a_batch_anthropic_no_longer_has_ends_only_itself(conn, data_dir, job, models, batches, monkeypatch):
    config = settings(data_dir, writer_=OPUS)
    gone = set()

    def ended(client, role, key, batch):
        if batch in gone:
            raise llm.Expired(runner.anthropic.GONE)
        return batch in batches["ended"]

    monkeypatch.setattr(runner.anthropic, "batch_ended", ended)
    flow.start(conn, config, job)
    run(conn, config)
    flow.start(conn, config, add_job(conn, "Another Job"))
    run(conn, config)
    assert batches["sent"] == [["aa-1"], ["aa-2"]]
    gone.add("b1")  # unknown at Anthropic now (e.g. the key changed to another account)
    batches["ended"].add("b2")
    batches["results"]["b2"] = {"aa-2": llm.Reply(GOOD, USED)}
    run(conn, config, now=NOW + timedelta(minutes=2))
    assert batches["deleted"] == ["b2"] and one(conn, "SELECT status FROM preparations WHERE id = 2")[0] == "done"
    assert conn.execute("SELECT batch_id, status, saved_on IS NULL FROM batches ORDER BY id").fetchall() == [
        ("b1", "deleted", 1), ("b2", "deleted", 0), ("b3", "sent", 1),
    ]  # fmt: skip
    assert batches["sent"][2] == ["aa-1"]  # b1's call sent again with the next batch: not charged, not a try
    assert one(conn, "SELECT cost, tries, waits, batch_id FROM steps WHERE id = 1") == (0, 1, 1, "b3")


def test_a_batch_whose_delete_failed_is_deleted_at_a_later_pass(conn, data_dir, job, models, batches, monkeypatch):
    config = settings(data_dir, writer_=OPUS)
    failing = [llm.Busy("Anthropic is busy")]

    def delete(client, role, key, batch):
        if failing:
            raise failing.pop()
        batches["deleted"].append(batch)

    monkeypatch.setattr(runner.anthropic, "delete_batch", delete)
    flow.start(conn, config, job)
    run(conn, config)
    batches["ended"].add("b1")
    batches["results"]["b1"] = {"aa-1": llm.Reply(GOOD, USED)}
    run(conn, config)
    assert one(conn, "SELECT status FROM batches")[0] == "saved" and one(conn, STATE)[0] == "done"
    run(conn, config, now=NOW + timedelta(minutes=2))  # nothing else to do: the delete is tried again
    assert batches["deleted"] == ["b1"] and one(conn, "SELECT status FROM batches")[0] == "deleted"


def test_a_batch_anthropic_refuses_uses_a_try_each_time(conn, data_dir, job, models, batches, monkeypatch):
    config = settings(data_dir, writer_=OPUS)
    refused = llm.LLMError("Anthropic refused the request (status 400).")
    monkeypatch.setattr(runner.anthropic, "send_batch", lambda *args: (_ for _ in ()).throw(refused))
    flow.start(conn, config, job)
    run(conn, config)
    run(conn, config)  # sent again once
    assert one(conn, "SELECT status, tries, reserved, cost FROM steps") == ("failed", 2, 0, 0)  # not charged
    reasons = [reason for (reason,) in conn.execute("SELECT reason FROM failed_tries")]
    assert reasons == ["Anthropic refused the batch: Anthropic refused the request (status 400)."] * 2
    run(conn, config)
    assert one(conn, STATE)[0] == "by_hand"  # never sent again and again


class AtOnce:
    """Cloud calls answered the moment they are sent, so the order answers are saved in is plain to see."""

    def __init__(self, workers):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *error):
        return False

    def submit(self, ask, *args):
        future = Future()
        future.set_result(ask(*args))
        return future


def test_cloud_answers_are_saved_before_the_next_laptop_call(conn, data_dir, job, models, monkeypatch):
    config = settings(data_dir, truth=SOL)
    seen, call = [], runner.llm.call
    monkeypatch.setattr(runner, "ThreadPoolExecutor", AtOnce)
    monkeypatch.setattr(runner.llm, "call", lambda role, *args, **kwargs: seen.append(one(
        conn, "SELECT status FROM steps WHERE member = 'truth'")) or call(role, *args, **kwargs))  # fmt: skip
    flow.start(conn, config, job)
    run(conn, config)
    assert seen[1:3] == [("sent",), ("done",)]  # the Truth checker's answer saved before the laptop's next call


def test_a_part_4b_version_left_writing_finishes_as_writer_only(conn, data_dir, job, models):
    config = settings(data_dir)
    old = flow.start(conn, config, job, writer_only=True)
    with conn:
        conn.execute("UPDATE preparations SET council = NULL WHERE id = ?", (old,))  # as the v15 upgrade keeps it
    run(conn, config)
    assert one(conn, "SELECT status, agreed, problem FROM preparations WHERE id = ?", old) == ("done", None, None)


def test_an_unexpected_error_ends_only_its_own_version(conn, data_dir, job, models, batches, monkeypatch):
    config = settings(data_dir)
    broken, other = flow.start(conn, config, job), flow.start(conn, config, add_job(conn, "Another Job"))
    with conn:
        conn.execute("UPDATE preparations SET asked = 'damaged' WHERE id = ?", (broken,))
    run(conn, config)
    assert one(conn, "SELECT status, problem FROM preparations WHERE id = ?", broken) == (
        "by_hand", runner.UNEXPECTED.format("JSONDecodeError"),
    )  # fmt: skip
    assert one(conn, "SELECT status, agreed FROM preparations WHERE id = ?", other) == ("done", 1)
    in_batch = flow.start(conn, settings(data_dir, writer_=OPUS), add_job(conn, "A Third Job"))
    run(conn, config)
    with conn:
        conn.execute("UPDATE preparations SET asked = 'damaged' WHERE id = ?", (in_batch,))
    batches["ended"].add("b1")
    step = one(conn, "SELECT id FROM steps WHERE preparation_id = ?", in_batch)[0]
    batches["results"]["b1"] = {f"aa-{step}": llm.Reply(GOOD, USED)}
    run(conn, config)  # collected: its version can't be checked, so it ends; the batch is still saved and deleted
    assert one(conn, "SELECT status FROM preparations WHERE id = ?", in_batch)[0] == "by_hand"
    assert one(conn, "SELECT status FROM steps WHERE id = ?", step)[0] == "failed"
    assert one(conn, "SELECT status FROM batches")[0] == "deleted"
    monkeypatch.setattr(flow, "advance", lambda *args: (_ for _ in ()).throw(runner.sqlcipher3.OperationalError()))
    flow.start(conn, config, add_job(conn, "A Fourth Job"))
    with pytest.raises(runner.sqlcipher3.OperationalError):  # about the whole database: the pass stops
        run(conn, config)


def test_a_stopped_version_sends_nothing_more_until_you_continue(conn, data_dir, job, models, fake_keychain):
    calls, scripted = models
    fake_keychain.store[(SERVICE, "openai")] = "fake-key"
    config = settings(data_dir, writer_=SOL)
    version = flow.start(conn, config, job)
    scripted["writer"] = [llm.Busy("OpenAI is busy (status 429)")]
    run(conn, config)
    with conn:
        conn.execute("UPDATE jobs SET status = 'closed'")
    run(conn, config, now=NOW + timedelta(minutes=10))
    assert len(calls) == 1 and one(conn, STATE)[0] == "stopped" and one(conn, STEPS)[2] == "waiting"
    with conn:  # Continue anyway
        conn.execute("UPDATE preparations SET status = 'writing', waived = '[\"closed\"]' WHERE id = ?", (version,))
    run(conn, config, now=NOW + timedelta(minutes=10))
    assert calls[1]["member"] == "writer" and one(conn, STATE)[0] == "done"


def test_a_call_a_stopped_runner_left_out_is_counted_and_sent_again(conn, data_dir, job, models):
    config = settings(data_dir)

    flow.start(conn, config, job)
    with conn:
        conn.execute("INSERT INTO steps (preparation_id, service, model, mode, status, cost, reserved, day, sent_on) "
                     "VALUES (1, 'ollama', 'm', 'direct', 'sent', 0, 0.25, 'd', 'now')")  # fmt: skip
    run(conn, config)
    assert one(conn, "SELECT status, tries, cost FROM steps WHERE id = 1") == ("done", 2, 0.25)
    assert one(conn, "SELECT reason FROM failed_tries")[0].startswith("Interrupted: the runner stopped")


def test_laptop_calls_wait_while_scoring_uses_the_graphics_card(
    conn, data_dir, job, models, monkeypatch, fake_keychain
):
    calls, _ = models
    monkeypatch.undo()
    calls.clear()
    fake_keychain.store[(SERVICE, "openai")] = "fake-key"
    monkeypatch.setattr(runner.llm, "call", lambda role, *a, **k: calls.append(role.model) or llm.Reply(GOOD, USED))
    config = settings(data_dir, writer_=SOL)

    flow.start(conn, config, job)
    with closing(collect.lock(data_dir, score.SCORE_LOCK, score.BUSY)):  # a scoring run
        run(conn, config)
    assert calls == ["gpt-6.1-sol"]  # the cloud Writer went; the laptop reviewers wait
    assert runner.laptop_work(conn)
