"""One press of Prepare, step by step, against a fake Writer: what is saved, what is counted, and every way it stops."""

import json

import pytest
from helpers import add_job, one
from test_privacy import CONTACT
from test_tiles import RESUME_CLS
from test_writer import LAB, PROFILE, answer, bullet

from app import llm, questionnaire
from app.config import AIRole, AISettings, Config, SpendingSettings
from app.council import runs, writer
from app.documents import tile_sets
from app.documents.tiles import Tile
from app.keychain import KeychainError
from app.resume import RESUME_FILE

OPUS = AIRole(service="anthropic", model="claude-opus-5-5", key_name="anthropic")
LOCAL = AIRole(model="gemma4:26b-a4b-it-q8_0")
OWN = AIRole(service="openai-compatible", model="my-model", address="https://models.example.org/v1")
GOOD = answer(("E2", [bullet("Trained a robot arm with PPO.", 0)], []))
BAD = answer(("E99", [bullet("Trained a robot arm with PPO.", 0)], []))
USED = llm.Usage(2000, 0, 500)  # Opus: 2000 x $4 + 500 x $20 per 1M = $0.018
STEP = "SELECT status, tries, input_tokens, output_tokens, cost, reserved, error FROM steps"


@pytest.fixture
def job(conn, data_dir):
    (data_dir / RESUME_FILE).write_text(RESUME_CLS, encoding="utf-8")
    questionnaire.save(conn, questionnaire.check({"contact": CONTACT}))
    tile_sets.confirm(conn, PROFILE, "fingerprint-1")
    with conn:
        return add_job(conn, "Robot Learning Intern", description="Train robots with PPO.")


@pytest.fixture
def writer_answers(monkeypatch):
    """The fake Writer: give it replies (a Reply, an answer's text, or an error to raise); it records each call."""
    calls, replies = [], []

    def call(role, system, user, shape, *, retry, answer_limit, timeout):
        calls.append({"user": user, "retry": retry, "limit": answer_limit, "timeout": timeout, "shape": shape})
        reply = replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply if isinstance(reply, llm.Reply) else llm.Reply(reply, USED)

    monkeypatch.setattr(runs.llm, "call", call)
    return calls, replies


def config(data_dir, role=OPUS, daily=15):
    return Config(data_dir=data_dir, ai=AISettings(writer=role), spending=SpendingSettings(daily=daily))


def version(conn):
    return runs.latest(conn, 1)


def test_a_good_first_answer_is_saved_with_its_cost(conn, data_dir, job, writer_answers):
    calls, replies = writer_answers
    replies.append(GOOD)
    runs.prepare(conn, config(data_dir), job)
    found = version(conn)
    assert (found["version"], found["status"], found["problem"]) == (1, "done", None)
    assert json.loads(found["draft"])["entries"][0]["tile_id"] == "exp-1"
    assert one(conn, STEP) == ("done", 1, 2000, 500, pytest.approx(0.018), 0, None)
    assert json.loads(found["sources"]) == [{"source": "profile", "set": 1}]
    saved = json.loads(one(conn, "SELECT writer FROM preparations")[0])
    assert (saved["model"], saved["key_name"]) == ("claude-opus-5-5", "anthropic")  # the key's name, never a key
    assert (calls[0]["limit"], calls[0]["timeout"], calls[0]["retry"]) == (8192, 600, False)
    assert calls[0]["shape"] is writer.SHAPE and "Alex" not in calls[0]["user"]


def test_a_bad_answer_is_retried_once_and_told_what_was_wrong(conn, data_dir, job, writer_answers):
    calls, replies = writer_answers
    replies += [BAD, GOOD]
    runs.prepare(conn, config(data_dir), job)
    assert version(conn)["status"] == "done"
    assert one(conn, STEP) == ("done", 2, 4000, 1000, pytest.approx(0.036), 0, None)
    assert calls[1]["retry"] and calls[1]["user"].endswith("Answer again in full, following every rule.")
    assert "Your previous answer was refused: an entry's id is not one of the labels" in calls[1]["user"]


def test_two_failed_tries_need_checking_by_hand_and_both_are_counted(conn, data_dir, job, writer_answers):
    _, replies = writer_answers
    replies += [BAD, llm.LLMError("the answer was cut off at its length limit", llm.Usage(1000, 0, 8192))]
    runs.prepare(conn, config(data_dir), job)
    found = version(conn)
    assert found["status"] == "by_hand" and found["draft"] is None
    assert found["problem"] == (
        "Writer failed twice (the answer was refused: an entry's id is not one of the labels E1, E2, ... given; then "
        "the answer was cut off at its length limit): check by hand, or press Retry."
    )
    status, tries, _, output, cost, reserved, error = one(conn, STEP)
    assert (status, tries, output, reserved, error) == (
        "failed",
        2,
        8692,
        0,
        "the answer was cut off at its length limit",
    )
    assert cost == pytest.approx(0.018 + (1000 * 4 + 8192 * 20) / 1e6)  # the cut-off answer was charged too


def test_no_answer_in_time_counts_what_was_set_aside_then_retries(conn, data_dir, job, writer_answers):
    _, replies = writer_answers
    replies += [llm.TimedOut("no answer within 600 seconds"), GOOD]
    runs.prepare(conn, config(data_dir), job)
    assert version(conn)["status"] == "done"
    cost = one(conn, "SELECT cost FROM steps")[0]
    assert cost > 0.3 + 0.018  # the whole largest possible cost (about $0.33) of the first try, plus the second


def test_the_local_model_is_free_even_when_it_times_out(conn, data_dir, job, writer_answers):
    _, replies = writer_answers
    replies += [llm.TimedOut("no answer within 600 seconds"), GOOD]
    runs.prepare(conn, config(data_dir, LOCAL), job)
    assert one(conn, "SELECT status, cost, input_tokens FROM steps") == ("done", 0, 2000)


def test_your_own_address_without_a_price_is_not_counted(conn, data_dir, job, writer_answers):
    writer_answers[1].append(GOOD)
    runs.prepare(conn, config(data_dir, OWN), job)
    assert one(conn, "SELECT status, cost, input_tokens FROM steps") == ("done", None, 2000)


@pytest.mark.parametrize(
    ("error", "problem"),
    [
        (llm.StopScoring("Anthropic refused the key (status 401): check the key saved in the keychain."),
         "Anthropic refused the key (status 401)"),
        (llm.StopScoring("No key is saved in the keychain under the name 'anthropic'."), "No key is saved"),
        (llm.TooLong("the texts are longer than the model can read at once"),
         "Too long for the model (the texts are longer than the model can read at once): raise the working size"),
        (KeychainError("Could not read 'anthropic' from the OS keychain (KeyringLocked)."), "Could not read"),
    ],
)  # fmt: skip
def test_a_problem_a_retry_cant_fix_stops_after_one_call(conn, data_dir, job, writer_answers, error, problem):
    calls, replies = writer_answers
    replies += [error, GOOD]
    runs.prepare(conn, config(data_dir), job)
    found = version(conn)
    assert found["status"] == "stopped" and found["problem"].startswith(problem) and len(calls) == 1
    assert one(conn, "SELECT status, cost, reserved FROM steps") == ("failed", 0, 0)


def test_a_spending_limit_stops_before_anything_is_sent(conn, data_dir, job, writer_answers):
    calls, replies = writer_answers
    replies.append(GOOD)
    runs.prepare(conn, config(data_dir), job)
    with conn:
        conn.execute("UPDATE steps SET cost = 0.9")  # as if that call had cost $0.90
    calls.clear()
    runs.prepare(conn, config(data_dir, daily=1), job)  # this call could cost up to about $0.33
    found = version(conn)
    assert found["version"] == 2 and found["status"] == "stopped" and calls == []
    assert found["problem"].startswith("Paused: today's spending limit of $1.00 would be passed ($0.90 spent today")
    assert one(conn, "SELECT count(*) FROM steps") == (1,)  # nothing written down for the call never sent


@pytest.fixture
def never_called(writer_answers):
    yield writer_answers[0]
    assert writer_answers[0] == []


def test_a_privacy_stop_names_the_entry_and_sends_nothing(conn, data_dir, job, never_called):
    leaky = Tile("proj-2", "project", "Projects", {"name": "Fake Drone"}, ("Call the lab at 555-010-0123.",))
    tile_sets.confirm(conn, [LAB, leaky], "fingerprint-2")
    runs.prepare(conn, config(data_dir), job)
    assert version(conn)["problem"] == (
        "Privacy check stopped this: a phone number in entry 'Fake Drone'. Edit that entry, then press Prepare again."
    )
    assert one(conn, "SELECT count(*) FROM steps") == (0,)


def test_without_your_name_in_the_questionnaire_nothing_is_sent(conn, data_dir, job, never_called):
    questionnaire.save(conn, questionnaire.check({}))
    runs.prepare(conn, config(data_dir), job)
    assert version(conn)["status"] == "stopped" and "Fill in your full name" in version(conn)["problem"]


def test_an_unexpected_error_never_leaves_a_version_writing(conn, data_dir, job, writer_answers):
    writer_answers[1].append(ValueError("odd"))
    with pytest.raises(ValueError):
        runs.prepare(conn, config(data_dir), job)
    assert version(conn)["problem"] == "Something went wrong (ValueError): press Retry."


def test_each_press_is_a_new_version_and_a_change_of_entries_makes_the_last_one_outdated(conn, data_dir, job,
                                                                                          writer_answers):  # fmt: skip
    writer_answers[1].extend([GOOD, answer(("E1", [bullet("Trained a robot arm.", 0)], []))])  # E1: now the only one
    runs.prepare(conn, config(data_dir), job)
    assert not runs.outdated(conn, job, version(conn))
    tile_sets.confirm(conn, [LAB], "fingerprint-2")
    assert runs.outdated(conn, job, version(conn))
    runs.prepare(conn, config(data_dir), job)
    assert version(conn)["version"] == 2 and not runs.outdated(conn, job, version(conn))


def test_calls_a_closed_dashboard_left_waiting_are_marked_never_resent(conn, job):
    with conn:
        for number, cost, reserved in ((1, 0, 0.4), (2, 0, 0), (3, None, 0), (4, 0, None)):  # paid, free, not
            # counted (your own address without a price), and none sent yet
            conn.execute(
                "INSERT INTO preparations (job_id, kind, version, sources, writer, status, created_on) "
                "VALUES (?, 'resume', 1, '[]', '{}', 'writing', 'now')", (add_job(conn, f"Job {number}"),),
            )  # fmt: skip
            if reserved is not None:
                conn.execute(
                    "INSERT INTO steps (preparation_id, service, model, status, cost, reserved, day, sent_on) "
                    "VALUES (?, 'anthropic', 'm', 'sent', ?, ?, 'today', 'now')", (number, cost, reserved),
                )  # fmt: skip
    runs.recover(conn)
    problems = [problem for (problem,) in conn.execute("SELECT problem FROM preparations ORDER BY id")]
    closed = "Interrupted: the dashboard closed while the Writer was working"
    assert (
        problems == [f"{closed}; it may have been charged up to $0.40. Press Retry."] + [f"{closed}. Press Retry."] * 3
    )
    assert conn.execute("SELECT status, cost, reserved FROM steps ORDER BY id").fetchall() == [
        ("interrupted", 0.4, 0), ("interrupted", 0, 0), ("interrupted", None, 0)
    ]  # fmt: skip
    assert one(conn, "SELECT count(*) FROM preparations WHERE status = 'by_hand'") == (4,)


@pytest.mark.parametrize(("role", "cost"), [(OPUS, pytest.approx(0.33, abs=0.01)), (LOCAL, 0), (OWN, None)])
def test_a_call_cut_off_by_closing_the_dashboard_is_counted_honestly(conn, data_dir, job, writer_answers, role, cost):
    writer_answers[1].append(KeyboardInterrupt())  # the dashboard closes while the call waits
    with pytest.raises(KeyboardInterrupt):
        runs.prepare(conn, config(data_dir, role), job)
    assert one(conn, "SELECT status FROM steps") == ("sent",) and version(conn)["status"] == "writing"
    runs.recover(conn)
    assert one(conn, "SELECT status, cost, reserved FROM steps") == ("interrupted", cost, 0)
    assert version(conn)["status"] == "by_hand"  # paid: its largest possible cost; free: $0; unpriced: not counted
