"""The council's rounds for one version, with a made-up person, job and answers: the tests play the runner's part,
saving each answer as a finished step (nothing here reaches a real AI)."""

import json

import pytest
from helpers import add_job, one
from test_privacy import CONTACT
from test_reviewers import QUOTE, review
from test_runs import GOOD, LOCAL
from test_tiles import RESUME_CLS
from test_writer import PROFILE, answer, bullet

from app import questionnaire
from app.config import AISettings, Config, CouncilSettings, CustomMember
from app.council import flow, letter, reviewers, runs, writer
from app.documents import tile_sets
from app.resume import RESUME_FILE, from_latex

MEMBERS = ("truth", "hiring", "editor", "ats", "domain")
APPROVE = review()  # no objections
STATE = "SELECT status, round, agreed, wording, problem FROM preparations"
STEP = """INSERT INTO steps (preparation_id, round, member, turn, service, model, status, answer, day, sent_on)
    VALUES (?, ?, ?, ?, 'ollama', 'm', ?, ?, '2026-11-01', ?)"""


@pytest.fixture
def job(conn, data_dir):
    """A made-up job, Profile (confirmed for the made-up resume) and Questionnaire."""
    (data_dir / RESUME_FILE).write_text(RESUME_CLS, encoding="utf-8")
    questionnaire.save(conn, questionnaire.check({"contact": CONTACT}))
    tile_sets.confirm(conn, PROFILE, from_latex(RESUME_CLS).fingerprint)
    with conn:
        return add_job(conn, "Robot Learning Intern", description="Train robots with PPO.")


@pytest.fixture(autouse=True)
def built(monkeypatch):
    """The PDF builds, recorded instead of running Tectonic: (target, LaTeX)."""
    found = []
    monkeypatch.setattr(flow.pdf, "build_kept", lambda data_dir, target, source: found.append((target, source)))
    return found


def settings(data_dir, rounds=3, turns=1, extra=()):
    roles = dict.fromkeys(("writer", *MEMBERS), LOCAL)
    return Config(data_dir=data_dir, ai=AISettings(**roles, extra=list(extra)),
                  council=CouncilSettings(rounds=rounds, turns=turns))  # fmt: skip


def finish(conn, call, text=None, sent_on="2026-11-01T10:00:00+00:00"):
    """The runner's part: the call answered (checked as the runner would) or, without text, no valid answer."""
    status, kept = ("done", json.dumps(call.check(text))) if text else ("failed", None)
    with conn:
        conn.execute(STEP, (call.preparation, call.round, call.member, call.turn, status, kept, sent_on))


def answer_all(conn, calls, texts):
    for call in calls:
        finish(conn, call, texts.get(call.member, APPROVE) if isinstance(texts, dict) else texts)


def test_a_version_starts_with_its_members_as_they_are_now(conn, data_dir, job):
    added = CustomMember(id="x1", name="Recruiter", letters=True, role=LOCAL)
    config = settings(data_dir, extra=[added, CustomMember(id="x2", name="Scout", role=LOCAL)])
    first = flow.start(conn, config, job, waived=frozenset({"closed"}), mode="normal")
    council = json.loads(one(conn, "SELECT council FROM preparations WHERE id = ?", first)[0])
    assert list(council["members"]) == [*MEMBERS, "x1", "x2"] and council["names"] == {"x1": "Recruiter", "x2": "Scout"}
    assert one(conn, "SELECT waived, mode, round, status FROM preparations WHERE id = ?", first) == (
        '["closed"]', "normal", 1, "writing",
    )  # fmt: skip
    letter_ = flow.start(conn, config, job, runs.LETTER)
    assert list(json.loads(one(conn, "SELECT council FROM preparations WHERE id = ?", letter_)[0])["members"]) == [
        "truth", "hiring", "editor", "x1",
    ]  # fmt: skip


def test_a_round_is_the_writer_then_each_reviewer_alone_then_agreed(conn, data_dir, job, built):
    config = settings(data_dir)
    version = flow.start(conn, config, job)
    [call] = flow.advance(conn, config, version)
    assert (call.round, call.member, call.turn, call.shape, call.limit) == (1, "writer", 0, writer.SHAPE, 8192)
    assert "<entries>" in call.user and "<council>" not in call.user
    finish(conn, call, GOOD)
    calls = flow.advance(conn, config, version)
    assert [(c.member, c.turn, c.shape) for c in calls] == [(m, 0, reviewers.SHAPE) for m in MEMBERS]
    assert "E2.1: Trained a robot arm with PPO." in calls[0].user and len(built) == 1  # draft kept, PDF for pages
    answer_all(conn, calls, APPROVE)
    assert flow.advance(conn, config, version) == []
    assert one(conn, STATE) == ("done", 1, 1, None, None) and len(built) == 2  # no discussion needed


def test_calls_already_sent_are_waited_for(conn, data_dir, job):
    config = settings(data_dir)
    version = flow.start(conn, config, job)
    [call] = flow.advance(conn, config, version)
    with conn:
        conn.execute(STEP, (version, 1, "writer", 0, "sent", None, "now"))
    assert flow.advance(conn, config, version) == [] and one(conn, STATE)[0] == "writing"
    with conn:
        conn.execute("UPDATE steps SET status = 'done', answer = ?", (json.dumps(call.check(GOOD)),))
        conn.execute(STEP, (version, 1, "truth", 0, "waiting", None, "now"))
    assert [c.member for c in flow.advance(conn, config, version)] == ["hiring", "editor", "ats", "domain"]
    for member in ("hiring", "editor", "ats", "domain"):
        with conn:
            conn.execute(STEP, (version, 1, member, 0, "done", json.dumps(json.loads(APPROVE) | {"renumbered": []}),
                                "now"))  # fmt: skip
    assert flow.advance(conn, config, version) == [] and one(conn, STATE)[0] == "writing"  # the Truth checker's


def test_an_ended_version_needs_nothing_more(conn, data_dir, job):
    config = settings(data_dir)
    version = flow.start(conn, config, job)
    with conn:
        conn.execute("UPDATE preparations SET status = 'stopped'")
    assert flow.advance(conn, config, version) == []


def test_a_suggestion_is_discussed_and_a_change_is_discussed_again(conn, data_dir, job):
    config = settings(data_dir, turns=2)
    version = flow.start(conn, config, job)
    finish(conn, flow.advance(conn, config, version)[0], GOOD)
    should = review({"severity": "should", "quote": ""})  # approve, with a suggestion: worth discussing
    answer_all(conn, flow.advance(conn, config, version), {"hiring": should})
    turn_1 = flow.advance(conn, config, version)
    assert {c.turn for c in turn_1} == {1}
    answer_all(conn, turn_1, {"hiring": APPROVE})  # the Hiring manager drops its suggestion: a change
    turn_2 = flow.advance(conn, config, version)
    assert {c.turn for c in turn_2} == {2}
    answer_all(conn, turn_2, APPROVE)  # nobody changed anything since: agreed
    assert flow.advance(conn, config, version) == [] and one(conn, STATE)[:3] == ("done", 1, 1)


def must(where="E2.1"):
    return review({"where": where})  # a must objection quoting your original


def test_objections_lead_to_a_discussion_then_a_revision_with_the_councils_update(conn, data_dir, job):
    config = settings(data_dir)
    version = flow.start(conn, config, job)
    finish(conn, flow.advance(conn, config, version)[0], GOOD)
    answer_all(conn, flow.advance(conn, config, version), {"truth": must()})
    discussion = flow.advance(conn, config, version)
    assert [(c.member, c.turn) for c in discussion] == [(m, 1) for m in MEMBERS]
    assert "<discussion>" in discussion[0].user and "TC-1 | must | E2.1" in discussion[1].user
    answer_all(conn, discussion, {"truth": review({"id": "TC-1", "problem": "Still 95%."})})  # its last word
    [revision] = flow.advance(conn, config, version)
    assert (revision.round, revision.member) == (2, "writer") and one(conn, "SELECT round FROM preparations")[0] == 2
    assert "<council>" in revision.user and "TC-1 | must | E2.1 | problem: Still 95%." in revision.user
    assert "<previous>" in revision.user
    with pytest.raises(writer.BadAnswer, match="one of the council's objections"):
        revision.check(answer(("E2", [GOOD_BULLET], []), responses=[{"objection": "HM-9", "action": "fixed",
                                                                     "why": ""}]))  # fmt: skip
    finish(conn, revision, answer(("E2", [GOOD_BULLET], []), responses=[REPLY]))
    second = flow.advance(conn, config, version)
    assert "<debate>" in second[0].user and "The Writer's replies:\nTC-1: fixed (Said 90%.)" in second[0].user


GOOD_BULLET = bullet("Trained a robot arm with PPO.", 0)
REPLY = {"objection": "TC-1", "action": "fixed", "why": "Said 90%."}


def test_no_agreement_keeps_your_original_wording_where_the_truth_checker_objects(conn, data_dir, job, built):
    config = settings(data_dir, rounds=1, turns=0)
    version = flow.start(conn, config, job)
    finish(conn, flow.advance(conn, config, version)[0], GOOD)
    answer_all(conn, flow.advance(conn, config, version), {"truth": must(), "hiring": review({"quote": ""})})
    assert flow.advance(conn, config, version) == []
    assert one(conn, STATE) == ("done", 1, 0, "original", None)
    assert json.loads(one(conn, "SELECT draft FROM preparations")[0])["objected"] == [["profile", "exp-1"]]
    assert QUOTE.replace("%", r"\%") in built[-1][1] and "Trained a robot arm with PPO." not in built[-1][1]
    with conn:
        conn.execute("UPDATE preparations SET wording = 'writer'")  # the switch: the Writer's wording after all
    assert "Trained a robot arm with PPO." in runs.pdf_source(conn, config, version)[1]


def test_without_a_valid_answer_from_the_truth_checker_the_writers_wording_stays_flagged(conn, data_dir, job):
    config = settings(data_dir, rounds=1, turns=0)
    version = flow.start(conn, config, job)
    finish(conn, flow.advance(conn, config, version)[0], GOOD)
    for call in flow.advance(conn, config, version):
        finish(conn, call, None if call.member == "truth" else APPROVE)
    flow.advance(conn, config, version)
    assert one(conn, STATE) == ("done", 1, 0, "writer", None)
    objected = json.loads(one(conn, "SELECT draft FROM preparations")[0])["objected"]
    assert objected == [["profile", "sum-1"], ["profile", "exp-1"]]  # every entry with rewritten bullets


def test_a_whole_resume_objection_keeps_your_wording_everywhere(conn, data_dir, job):
    config = settings(data_dir, rounds=1, turns=0)
    version = flow.start(conn, config, job)
    finish(conn, flow.advance(conn, config, version)[0], GOOD)
    answer_all(conn, flow.advance(conn, config, version), {"truth": must("")})
    flow.advance(conn, config, version)
    assert one(conn, STATE)[3] == "original"
    assert len(json.loads(one(conn, "SELECT draft FROM preparations")[0])["objected"]) == 2


def test_the_writer_failing_twice_needs_checking_by_hand(conn, data_dir, job):
    config = settings(data_dir)
    version = flow.start(conn, config, job)
    finish(conn, flow.advance(conn, config, version)[0])
    assert flow.advance(conn, config, version) == []
    assert one(conn, STATE)[:1] + one(conn, STATE)[4:] == ("by_hand", flow.NO_WRITER.format(1))


@pytest.mark.parametrize(
    ("change", "waived", "problem"),
    [
        ("UPDATE jobs SET status = 'closed'", (), flow.CLOSED),
        ("UPDATE jobs SET not_interested = 1", (), flow.SKIPPED),
        ("UPDATE jobs SET status = 'closed'", ("closed",), None),  # you proceeded anyway
    ],
)
def test_a_closed_or_skipped_job_stops_before_its_next_step(conn, data_dir, job, change, waived, problem):
    config = settings(data_dir)
    version = flow.start(conn, config, job, waived=frozenset(waived))
    with conn:
        conn.execute(change)
    calls = flow.advance(conn, config, version)
    assert (calls == []) is (problem is not None) and one(conn, STATE)[4] == problem


def test_entries_changed_mid_council_stop_it(conn, data_dir, job):
    config = settings(data_dir)
    version = flow.start(conn, config, job)
    finish(conn, flow.advance(conn, config, version)[0], GOOD)
    tile_sets.confirm(conn, PROFILE[:3], from_latex(RESUME_CLS).fingerprint)  # a new confirmed set
    assert flow.advance(conn, config, version) == []
    assert one(conn, STATE)[0::4] == ("stopped", flow.CHANGED)


def test_your_words_after_the_last_writer_add_a_round(conn, data_dir, job):
    config = settings(data_dir, rounds=1, turns=0)
    version = flow.start(conn, config, job)
    finish(conn, flow.advance(conn, config, version)[0], GOOD, sent_on="2026-11-01T10:00:00+00:00")
    with conn:
        conn.execute(
            "UPDATE preparations SET asked = ?", (json.dumps([{"words": "More Rust.", "on": "2026-11-01T11"}]),)
        )
    answer_all(conn, flow.advance(conn, config, version), APPROVE)
    [extra] = flow.advance(conn, config, version)  # agreed, but your words came after the Writer: one more round
    assert (extra.round, extra.member) == (2, "writer") and "<feedback>\nMore Rust.\n</feedback>" in extra.user


def test_a_resume_over_one_page_is_flagged_to_the_reviewers(conn, data_dir, job, monkeypatch):
    config = settings(data_dir)
    monkeypatch.setattr(flow.pdf, "built", lambda target: flow.pdf.Built(2, ()))
    version = flow.start(conn, config, job)
    finish(conn, flow.advance(conn, config, version)[0], GOOD)
    assert "- " + flow.ONE_PAGE.format(2) in flow.advance(conn, config, version)[0].user


def test_a_letter_is_reviewed_by_three_and_objected_paragraphs_are_left_out(conn, data_dir, job, built):
    config = settings(data_dir, rounds=1, turns=0)
    version = flow.start(conn, config, job, runs.LETTER)
    [call] = flow.advance(conn, config, version)
    assert call.shape == letter.SHAPE
    text = json.dumps({"paragraphs": ["I want this job.", "I won a prize.", "Thanks."], "from": ["E2"], "notes": "",
                       "responses": []})  # fmt: skip
    finish(conn, call, text)
    calls = flow.advance(conn, config, version)
    assert [c.member for c in calls] == ["truth", "hiring", "editor"] and "P2: I won a prize." in calls[0].user
    answer_all(conn, calls, {"truth": review({"where": "P2"})})
    flow.advance(conn, config, version)
    assert one(conn, STATE)[:4] == ("done", 1, 0, "original")
    assert "I won a prize." not in built[-1][1] and "Thanks." in built[-1][1]


def test_a_letter_objection_no_paragraph_can_answer_is_checked_by_hand(conn, data_dir, job):
    config = settings(data_dir, rounds=1, turns=0)
    version = flow.start(conn, config, job, runs.LETTER)
    finish(conn, flow.advance(conn, config, version)[0],
           json.dumps({"paragraphs": ["A.", "B.", "C."], "from": ["E2"], "notes": "", "responses": []}))  # fmt: skip
    answer_all(conn, flow.advance(conn, config, version), {"truth": must("E2")})
    flow.advance(conn, config, version)
    assert one(conn, STATE)[0::4] == ("by_hand", flow.LETTER_UNCLEAR)


def test_missing_instructions_or_a_privacy_stop_stop_the_version_before_sending(conn, data_dir, job):
    config = settings(data_dir, extra=[CustomMember(id="x1", name="Scout", role=LOCAL)])
    version = flow.start(conn, config, job)
    finish(conn, flow.advance(conn, config, version)[0], GOOD)
    assert flow.advance(conn, config, version) == []
    assert one(conn, STATE)[0] == "stopped" and "instructions for x1 are missing" in one(conn, STATE)[4]
    change = flow.start(conn, settings(data_dir), job, feedback="Call me at 555-010-0123.")
    with conn:
        conn.execute("UPDATE preparations SET based_on = ?, status = 'done' WHERE id = ?", (version, version))
        conn.execute("UPDATE preparations SET based_on = ? WHERE id = ?", (version, change))
    assert flow.advance(conn, settings(data_dir), change) == []
    assert one(conn, "SELECT problem FROM preparations WHERE id = ?", change)[0] == (
        "Privacy check stopped this: a phone number in your feedback. Change that, then try again."
    )


def test_the_discussion_stops_early_once_nobody_changes_anything(conn, data_dir, job):
    config = settings(data_dir, turns=3)
    version = flow.start(conn, config, job)
    finish(conn, flow.advance(conn, config, version)[0], GOOD)
    answer_all(conn, flow.advance(conn, config, version), {"hiring": review({"severity": "should", "quote": ""})})
    answer_all(conn, flow.advance(conn, config, version), {"hiring": review({"id": "HM-1", "severity": "should",
                                                                            "quote": ""})})  # fmt: skip
    assert flow.advance(conn, config, version) == []  # no turn 2: agreed, with the suggestion noted
    assert one(conn, STATE)[:3] == ("done", 1, 1)
