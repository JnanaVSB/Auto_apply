"""The Answer key page: marks, suggestions, model and threshold agreement, the pass-through table and scoring the
marked jobs with a chosen model, with made-up jobs."""

import pytest
from helpers import add_job, csrf, log_in, settings
from test_web_scoring import CHOSEN, MODEL, scored

from app.config import ScoringSettings
from app.web import answer_key, main
from app.web.answer_key import Counts, Route

OTHER = "other:model"


def mark(conn, job_id, label):
    conn.execute("INSERT INTO answer_key (job_id, label, marked_on) VALUES (?, ?, '2026-10-28')", (job_id, label))


@pytest.fixture
def jobs(conn):
    """Five marked jobs with their quick checks and scores, and unmarked jobs at every meaning band."""
    with conn:
        made = {name: add_job(conn, name) for name in ("Yes RL", "Yes", "Maybe", "No", "No changed")}
        scored(conn, made["Yes RL"], keywords=["RL"], meaning=0.80, score=85)
        scored(conn, made["Yes RL"], keywords=["RL"], meaning=0.80, score=60, model=OTHER)
        scored(conn, made["Yes"], meaning=0.62, score=72)
        scored(conn, made["Maybe"], meaning=0.52, failed="AI score failed twice: check this job by hand")
        scored(conn, made["No"], passed=0, meaning=0.48, score=30)
        scored(conn, made["No"], passed=0, meaning=0.48, score=90, model=OTHER, resume="resume-before")
        scored(conn, made["No changed"], passed=0, shared=(), meaning=0.40, score=20)
        conn.execute("UPDATE ai_scores SET changed = 1 WHERE job_id = ?", (made["No changed"],))
        for name, label in (("Yes RL", "yes"), ("Yes", "yes"), ("Maybe", "maybe"), ("No", "no"), ("No changed", "no")):
            mark(conn, made[name], label)
        for name, meaning, passed in (("Strong", 0.75, 1), ("Border", 0.60, 1), ("Weak", 0.30, 0)):
            made[name] = add_job(conn, name)
            scored(conn, made[name], meaning=meaning, passed=passed)
        scored(conn, add_job(conn, "Closed", status="no longer available"), meaning=0.9)
        scored(conn, add_job(conn, "Hidden", not_interested=1), meaning=0.9)
    return made


def test_marks_are_counted_and_suggestions_skip_marked_closed_and_hidden_jobs(conn, jobs):
    assert answer_key.marks(conn) == {"yes": 2, "maybe": 1, "no": 2}
    found = answer_key.suggestions(conn)
    assert [[job["title"] for job in band] for band in found.values()] == [["Strong"], ["Border"], ["Weak"]]


def test_ai_agreement_counts_only_current_scores_per_model_and_label(conn, jobs):
    assert answer_key.ai_agreement(conn, "resume-now") == {
        MODEL: {
            "yes": Counts(2, 0, (2, 2, 2, 1)), "maybe": Counts(0, 1, (0, 0, 0, 0)), "no": Counts(1, 0, (0, 0, 0, 0)),
        },
        OTHER: {"yes": Counts(1, 0, (1, 1, 0, 0))},  # its score made with an older resume does not count
    }  # fmt: skip
    assert answer_key.ai_agreement(conn, None) == {}  # no usable resume: no score counts


def test_a_failed_new_try_with_the_current_resume_counts_as_failed(conn, jobs):
    failure = (
        "INSERT INTO ai_failures (job_id, model, failed, resume, changed, tried_on) VALUES (?, ?, 'x', ?, ?, 'now')"
    )
    with conn:
        conn.execute(
            "UPDATE ai_scores SET resume = 'resume-before' WHERE job_id = ? AND model = ?", (jobs["Yes"], MODEL)
        )
        conn.execute(failure, (jobs["Yes"], MODEL, "resume-now", 0))  # counts as failed
        conn.execute(failure, (jobs["Yes RL"], MODEL, "resume-now", 0))  # its current score counts instead
        conn.execute(failure, (jobs["No"], OTHER, "resume-now", 1))  # the ad changed since: not counted
        conn.execute(failure, (jobs["No changed"], OTHER, "resume-before", 0))  # an older resume: not counted
    found = answer_key.ai_agreement(conn, "resume-now")
    assert found[MODEL]["yes"] == Counts(1, 1, (1, 1, 1, 1))
    assert found[OTHER] == {"yes": Counts(1, 0, (1, 1, 0, 0))}


def test_quick_agreement_at_each_meaning_threshold(conn, jobs):
    found = answer_key.quick_agreement(conn, ScoringSettings(meaning_threshold=0.52))
    assert list(found) == [0.45, 0.50, 0.52, 0.55, 0.60, 0.65]  # your own setting is always a row
    assert found[0.45] == {"yes": (2, 2), "maybe": (1, 1), "no": (2, 1)}
    assert found[0.65] == {"yes": (2, 1), "maybe": (1, 0), "no": (2, 0)}  # a keyword in the title still passes


def test_pass_through_by_route_with_the_chosen_model(conn, jobs):
    assert answer_key.pass_through(conn, CHOSEN) == [
        Route("your keyword in the title", 1, 1, 1),
        Route("meaning 0.50 to 0.60", 1, 0, 0),
        Route("meaning 0.60 to 0.70", 2, 1, 1),
        Route("meaning 0.70 and up", 2, 0, 0),  # a job you hid still went through the checks
    ]


@pytest.fixture
def client(client, monkeypatch, data_dir):
    monkeypatch.setattr(main, "_scoring", lambda: CHOSEN)
    monkeypatch.setattr(main, "load_config", lambda: settings(data_dir))
    monkeypatch.setattr(main.ollama, "installed", lambda role: [MODEL, OTHER])
    log_in(client)
    return client


@pytest.fixture
def starts(client, monkeypatch):
    calls = []
    monkeypatch.setattr(client.app.state.scorer, "start", lambda ids=None, model=None: calls.append((ids, model)))
    return calls


def test_the_answer_key_page(client, jobs):
    text = client.get("/answer-key").text
    assert '<a href="/answer-key">Answer key</a>' in text
    assert "You have marked 5 jobs: 2 yes, 1 maybe, 2 no." in text
    assert f"{MODEL} (the chosen model)" in text and "2 of 2" in text
    assert '"maybe": 0 scored, 1 failed, 0 not scored yet' in text
    assert f"<option selected>{MODEL}</option><option>{OTHER}</option>" in text
    assert "<td>your keyword in the title</td><td>1</td><td>1</td>" in text and "100%" in text


def test_score_my_marked_jobs_with_a_downloaded_model(client, conn, jobs, starts):
    with conn:
        mark(conn, add_job(conn, "Closed and marked", status="no longer available"), "yes")
    reply = client.post("/answer-key/score", data={"csrf_token": csrf(client), "model": OTHER}, follow_redirects=False)
    assert reply.headers["location"] == "/collect#scoring"
    marked = [jobs[name] for name in ("Yes RL", "Yes", "Maybe", "No", "No changed")]
    assert starts == [(marked, OTHER)]  # open marked jobs only


def test_a_model_ollama_does_not_have_is_refused(client, jobs, starts):
    reply = client.post("/answer-key/score", data={"csrf_token": csrf(client), "model": "rm -rf /"})
    assert reply.status_code == 400 and starts == []


def test_without_ollama_the_page_says_how_to_start_it(client, jobs, monkeypatch):
    monkeypatch.setattr(main.ollama, "installed", lambda role: [])
    assert "No models found: start Ollama with <code>ollama serve</code>" in client.get("/answer-key").text


def test_with_nothing_marked_or_scored_the_page_still_shows(client):
    text = client.get("/answer-key").text
    assert "You have marked 0 jobs" in text and "No job has passed the quick check yet." in text
