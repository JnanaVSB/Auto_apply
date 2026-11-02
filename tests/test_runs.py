"""One press of Prepare, step by step, against a fake Writer: what is saved, what is counted, and every way it stops."""

import json
from dataclasses import replace

import pytest
from helpers import add_job, one
from test_privacy import CONTACT
from test_tiles import RESUME_CLS
from test_writer import LAB, PROFILE, answer, bullet

from app import llm, questionnaire
from app.config import AIRole, AISettings, Config, SpendingSettings
from app.council import flow, letter, runner, runs, writer
from app.documents import tile_sets
from app.documents.tiles import Tile
from app.resume import RESUME_FILE, from_latex

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
    tile_sets.confirm(conn, PROFILE, from_latex(RESUME_CLS).fingerprint)
    with conn:
        return add_job(conn, "Robot Learning Intern", description="Train robots with PPO.")


def prepare(conn, config, job_id, feedback="", kind=runs.KIND):
    """One press of Resume only (the Writer alone), run to its end by the council's runner."""
    found = flow.start(conn, config, job_id, kind, feedback, mode="normal", writer_only=True)
    runner.run(conn, config, runner.State())
    return found


@pytest.fixture
def writer_answers(monkeypatch):
    """The fake Writer: give it replies (a Reply, an answer's text, or an error to raise); it records each call."""
    calls, replies = [], []

    def call(role, system, user, shape, *, retry, answer_limit, timeout, half=False):
        calls.append({"user": user, "retry": retry, "limit": answer_limit, "timeout": timeout, "shape": shape})
        reply = replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply if isinstance(reply, llm.Reply) else llm.Reply(reply, USED)

    monkeypatch.setattr(runs.llm, "call", call)
    return calls, replies


@pytest.fixture(autouse=True)
def built(monkeypatch):
    """Records the PDF builds instead of running Tectonic (never a download here); returns them as (target, LaTeX)."""
    found = []
    monkeypatch.setattr(runs.pdf, "build_kept", lambda data_dir, target, source: found.append((target, source)))
    return found


def config(data_dir, role=OPUS, daily=15):
    return Config(data_dir=data_dir, ai=AISettings(writer=role), spending=SpendingSettings(daily=daily))


def version(conn):
    return runs.latest(conn, 1)


def test_a_good_first_answer_is_saved_with_its_cost(conn, data_dir, job, writer_answers):
    calls, replies = writer_answers
    replies.append(GOOD)
    prepare(conn, config(data_dir), job)
    found = version(conn)
    assert (found["version"], found["status"], found["problem"]) == (1, "done", None)
    assert [item["tile_id"] for item in json.loads(found["draft"])["entries"]] == ["sum-1", "exp-1", "edu-1"]
    assert one(conn, STEP) == ("done", 1, 2000, 500, pytest.approx(0.018), 0, None)
    assert json.loads(found["sources"]) == [{"source": "profile", "set": 1}]
    saved = json.loads(one(conn, "SELECT writer FROM preparations")[0])
    assert (saved["model"], saved["key_name"]) == ("claude-opus-5-5", "anthropic")  # the key's name, never a key
    assert (calls[0]["limit"], calls[0]["timeout"], calls[0]["retry"]) == (8192, 600, False)
    assert calls[0]["shape"] is writer.SHAPE and "Alex" not in calls[0]["user"]


def test_a_bad_answer_is_retried_once_and_told_what_was_wrong(conn, data_dir, job, writer_answers):
    calls, replies = writer_answers
    replies += [BAD, GOOD]
    prepare(conn, config(data_dir), job)
    assert version(conn)["status"] == "done"
    assert one(conn, STEP) == ("done", 2, 4000, 1000, pytest.approx(0.036), 0, None)
    assert calls[1]["retry"] and calls[1]["user"].endswith("Answer again in full, following every rule.")
    assert "Your previous answer was refused: an entry's id is not one of the labels" in calls[1]["user"]


def test_two_failed_tries_need_checking_by_hand_and_both_are_counted(conn, data_dir, job, writer_answers):
    _, replies = writer_answers
    replies += [BAD, llm.LLMError("the answer was cut off at its length limit", llm.Usage(1000, 0, 8192))]
    prepare(conn, config(data_dir), job)
    found = version(conn)
    assert found["status"] == "by_hand" and found["draft"] is None
    assert found["problem"] == flow.NO_WRITER.format(1)  # each try's reason is kept with it (Failed tries)
    status, tries, _, output, cost, reserved, error = one(conn, STEP)
    assert (status, tries, output, reserved, error) == (
        "failed",
        2,
        8692,
        0,
        "the answer was cut off at its length limit",
    )
    assert cost == pytest.approx(0.018 + (1000 * 4 + 8192 * 20) / 1e6)  # the cut-off answer was charged too


FAILED = "SELECT try_number, output_tokens, cost, reason, reply, reply_length FROM failed_tries ORDER BY id"


def test_each_failed_try_is_kept_whole_with_what_came_back(conn, data_dir, job, writer_answers):
    _, replies = writer_answers
    cut = llm.LLMError("the answer was cut off at its length limit", llm.Usage(1000, 0, 8192))
    cut.reply = "x" * (runs.REPLY + 5)
    replies += [BAD, cut]
    prepare(conn, config(data_dir), job)
    (first, second) = conn.execute(FAILED).fetchall()
    assert first[:2] == (1, 500) and first[2] == pytest.approx(0.018) and first[4] == BAD and first[5] is None
    assert first[3] == "the answer was refused: an entry's id is not one of the labels E1, E2, ... given"  # whole
    assert second[:2] == (2, 8192) and second[3] == "the answer was cut off at its length limit"
    assert second[4] == "x" * runs.REPLY and second[5] == runs.REPLY + 5  # cut, saying how long it was
    assert runs.versions(conn, job)[0]["failed"] == 2
    listed = runs.failed_tries(conn, 10, 0)
    assert [found["try_number"] for found in listed] == [2, 1] and listed[0]["title"] == "Robot Learning Intern"
    assert runs.failed_tries(conn, 10, 0, version=99) == []


def test_a_failed_try_is_kept_even_when_the_retry_works(conn, data_dir, job, writer_answers):
    _, replies = writer_answers
    replies += [llm.TimedOut("no answer within 600 seconds"), GOOD]
    prepare(conn, config(data_dir), job)
    assert version(conn)["status"] == "done" and one(conn, "SELECT status FROM steps") == ("done",)
    ((number, _, cost, reason, reply, _),) = conn.execute(FAILED).fetchall()
    assert (number, reason, reply) == (1, "no answer within 600 seconds", None) and cost > 0.3  # what was set aside


def test_no_answer_in_time_counts_what_was_set_aside_then_retries(conn, data_dir, job, writer_answers):
    _, replies = writer_answers
    replies += [llm.TimedOut("no answer within 600 seconds"), GOOD]
    prepare(conn, config(data_dir), job)
    assert version(conn)["status"] == "done"
    cost = one(conn, "SELECT cost FROM steps")[0]
    assert cost > 0.3 + 0.018  # the whole largest possible cost (about $0.33) of the first try, plus the second


def test_the_local_model_is_free_even_when_it_times_out(conn, data_dir, job, writer_answers):
    _, replies = writer_answers
    replies += [llm.TimedOut("no answer within 600 seconds"), GOOD]
    prepare(conn, config(data_dir, LOCAL), job)
    assert one(conn, "SELECT status, cost, input_tokens FROM steps") == ("done", 0, 2000)


def test_your_own_address_without_a_price_is_not_counted(conn, data_dir, job, writer_answers):
    writer_answers[1].append(GOOD)
    prepare(conn, config(data_dir, OWN), job)
    assert one(conn, "SELECT status, cost, input_tokens FROM steps") == ("done", None, 2000)


@pytest.fixture
def never_called(writer_answers):
    yield writer_answers[0]
    assert writer_answers[0] == []


def test_a_privacy_stop_names_the_entry_and_sends_nothing(conn, data_dir, job, never_called):
    leaky = Tile("proj-2", "project", "Projects", {"name": "Fake Drone"}, ("Call the lab at 555-010-0123.",))
    tile_sets.confirm(conn, [LAB, leaky], from_latex(RESUME_CLS).fingerprint)
    prepare(conn, config(data_dir), job)
    assert version(conn)["problem"] == (
        "Privacy check stopped this: a phone number in entry 'Fake Drone'. Change that, then try again."
    )
    assert one(conn, "SELECT count(*) FROM steps") == (0,)


def test_without_your_name_in_the_questionnaire_nothing_is_sent(conn, data_dir, job, never_called):
    questionnaire.save(conn, questionnaire.check({}))
    prepare(conn, config(data_dir), job)
    assert version(conn)["status"] == "stopped" and "Fill in your full name" in version(conn)["problem"]


def test_each_press_is_a_new_version_and_a_change_of_entries_makes_the_last_one_outdated(conn, data_dir, job,
                                                                                          writer_answers):  # fmt: skip
    writer_answers[1].extend([GOOD, answer(("E1", [bullet("Trained a robot arm.", 0)], []))])  # E1: now the only one
    prepare(conn, config(data_dir), job)
    assert not runs.outdated(version(conn), runs.current(conn, job))
    tile_sets.confirm(conn, [LAB], from_latex(RESUME_CLS).fingerprint)
    assert runs.outdated(version(conn), runs.current(conn, job))
    prepare(conn, config(data_dir), job)
    assert version(conn)["version"] == 2 and not runs.outdated(version(conn), runs.current(conn, job))


def test_a_good_draft_becomes_a_pdf_with_your_details_put_back(conn, data_dir, job, writer_answers, built):
    writer_answers[1].append(answer(
        ("E2", [bullet("Shared the code at [LINK].", 2), bullet("Trained a robot arm with PPO.", 0)], []),
        ("E4", [], ["Rust", "Python"]),
    ))  # fmt: skip
    prepare(conn, config(data_dir), job)
    target, source = built[-1]
    assert target == data_dir / "documents" / str(job) / "resume-v1.pdf" and version(conn)["problem"] is None
    shared = source.index("Shared the code at https://github.com/alex-example.")  # as the Questionnaire writes it
    assert shared < source.index("Trained a robot arm with PPO.")  # the Writer's order inside an entry
    assert "Demo Robotics Lab" in source and "Jan 2024 – May 2025" in source  # the fixed facts, unchanged
    assert "Rust, Python" in source and "C++" not in source and "Fake Drone" not in source  # left out
    assert "Alex Example" in source and "[" not in source.split(r"\begin{document}")[1].replace("{[}", "")


def test_a_rebuilt_pdf_puts_back_what_each_placeholder_stood_for(conn, data_dir, job, writer_answers):
    writer_answers[1].append(answer(("E2", [bullet("Shared the code at [LINK].", 2)], [])))
    prepare(conn, config(data_dir), job)
    linkedin = "https://linkedin.com/in/alex-example"  # now [LINK]: your links are numbered from the Questionnaire's
    questionnaire.save(conn, questionnaire.check({"contact": CONTACT | {"linkedin": linkedin}}))
    _, source, note = runs.pdf_source(conn, config(data_dir), version(conn)["id"])
    assert "Shared the code at https://github.com/alex-example." in source and note is None
    assert "linkedin.com/in/alex-example" in source  # the header: the Questionnaire as it is now
    questionnaire.save(conn, questionnaire.check({"contact": CONTACT | {"github": "https://github.com/alex-new"}}))
    _, source, note = runs.pdf_source(conn, config(data_dir), version(conn)["id"])
    assert "Shared the code at https://github.com/alex-example." in source and "github.com/alex-new" in source
    assert note == (
        "PDF built, but a bullet still shows a link you have changed since: update that entry on the Resume Profile "
        "page, then press Prepare for a fresh version."
    )


def test_a_failed_build_keeps_the_draft_and_says_why(conn, data_dir, job, writer_answers, monkeypatch):
    def failing(data_dir, target, source):
        raise runs.pdf.PdfError("The LaTeX has an error (Undefined control sequence) on line 12.")

    monkeypatch.setattr(runs.pdf, "build_kept", failing)
    writer_answers[1].append(GOOD)
    prepare(conn, config(data_dir), job)
    found = version(conn)
    assert found["status"] == "done" and found["draft"]
    assert found["problem"] == "PDF could not be built: The LaTeX has an error (Undefined control sequence) on line 12."


def test_each_version_lists_its_cost(conn, data_dir, job, writer_answers, built):
    writer_answers[1].extend([GOOD, BAD, BAD])
    prepare(conn, config(data_dir), job)
    prepare(conn, config(data_dir, OWN), job)
    questionnaire.save(conn, questionnaire.check({}))
    prepare(conn, config(data_dir), job)  # stopped before any call: no name in the Questionnaire
    with conn:
        other = add_job(conn, "Another job")
    writer_answers[1].append(GOOD)
    prepare(conn, config(data_dir), other)
    found = runs.versions(conn, job)
    assert [(item["version"], item["status"], item["uncounted"]) for item in found] == [
        (3, "stopped", 0), (2, "by_hand", 1), (1, "done", 0)
    ]  # fmt: skip
    assert (found[2]["cost"], found[1]["cost"], found[0]["cost"]) == (pytest.approx(0.018), 0, 0)


def test_a_change_is_a_new_version_built_on_the_last_finished_one(conn, data_dir, job, writer_answers):
    calls, replies = writer_answers
    replies.extend([GOOD, GOOD])
    prepare(conn, config(data_dir), job)
    prepare(conn, config(data_dir), job, "Lead with the lab.")
    found = version(conn)
    assert (found["version"], found["status"], found["feedback"]) == (2, "done", "Lead with the lab.")
    assert one(conn, "SELECT based_on FROM preparations WHERE version = 2") == (1,)
    assert "<previous>\n" in calls[1]["user"] and calls[1]["user"].endswith(
        "<feedback>\nLead with the lab.\n</feedback>"
    )
    assert runs.versions(conn, job)[0]["feedback"] == "Lead with the lab."
    replies.extend([BAD, BAD, GOOD])
    prepare(conn, config(data_dir), job, "Shorter.")  # version 3 fails...
    prepare(conn, config(data_dir), job, "Shorter.")  # ...so version 4 builds on version 2, the last finished one
    assert one(conn, "SELECT based_on FROM preparations WHERE version = 4") == (
        one(conn, "SELECT id FROM preparations WHERE version = 2")[0],
    )


def test_a_change_to_a_version_over_one_page_says_so(conn, data_dir, job, writer_answers):
    calls, replies = writer_answers
    replies.extend([GOOD, GOOD])
    prepare(conn, config(data_dir), job)
    runs.pdf.tailored(data_dir, job, 1).parent.mkdir(parents=True, exist_ok=True)
    runs.pdf.tailored(data_dir, job, 1).write_bytes(b"%PDF")
    runs.pdf.tailored(data_dir, job, 1).with_suffix(".json").write_text('{"pages": 2, "missing": []}', "utf-8")
    prepare(conn, config(data_dir), job, "Tighter wording.")
    assert calls[1]["user"].endswith(
        "Tighter wording. The resume's PDF is 2 pages: it must fit on one page.\n</feedback>"
    )


def test_a_privacy_stop_in_your_words_sends_nothing(conn, data_dir, job, writer_answers):
    calls, replies = writer_answers
    replies.append(GOOD)
    prepare(conn, config(data_dir), job)
    prepare(conn, config(data_dir), job, "Add my number 555-010-0123.")
    assert (
        version(conn)["problem"]
        == "Privacy check stopped this: a phone number in your feedback. Change that, then try again."
    )
    assert len(calls) == 1


LETTER = json.dumps({"paragraphs": ["I want this job.", "I trained a robot arm with PPO.", "Thank you."],
                     "from": ["E1"], "notes": "Short.", "responses": []})  # fmt: skip


def test_a_cover_letter_is_its_own_kind_drawn_from_the_resumes_entries(conn, data_dir, job, writer_answers, built):
    calls, replies = writer_answers
    replies.extend([LETTER, GOOD, LETTER, LETTER])
    prepare(conn, config(data_dir), job, kind=runs.LETTER)  # no resume yet: all your entries
    assert "E6 | publication" in calls[0]["user"] and calls[0]["shape"] is letter.SHAPE
    prepare(conn, config(data_dir), job)  # the resume: summary, lab, school
    prepare(conn, config(data_dir), job, kind=runs.LETTER)
    assert "E3 | education" in calls[2]["user"] and "E4 |" not in calls[2]["user"]  # only what the resume chose
    found = runs.latest(conn, job, runs.LETTER)
    assert (found["version"], found["status"]) == (2, "done") and runs.latest(conn, job)["version"] == 1
    assert json.loads(found["draft"])["paragraphs"][1] == "I trained a robot arm with PPO."
    target, source = built[-1]
    assert target == data_dir / "documents" / str(job) / "cover-letter-v2.pdf"
    assert "Dear Hiring Manager," in source and "I trained a robot arm with PPO.\\par" in source
    assert "Re: Robot Learning Intern, Acme" in source and "Alex Example" in source
    changed = [*PROFILE[:2], replace(PROFILE[2], bullets=("Built a drone that maps whole rooms.",)), *PROFILE[3:]]
    tile_sets.confirm(conn, changed, from_latex(RESUME_CLS).fingerprint)  # the resume is now from older entries: all
    prepare(conn, config(data_dir), job, kind=runs.LETTER)
    assert "E6 | publication" in calls[3]["user"]
