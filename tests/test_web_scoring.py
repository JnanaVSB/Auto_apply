"""Scores on the dashboard: statuses, filters, counts, the Low score tab and the job page, with made-up jobs."""

import json

import pytest
from helpers import add_job, csrf, log_in, one

from app.web import main, queries

MODEL = queries.Scoring().model
CHOSEN = queries.Scoring(MODEL, 70, "resume-now")


def scored(conn, job_id, *, passed=1, score=None, failed=None, model=MODEL, reasons=(), odd=None, trick=None,
           resume="resume-now", keywords=(), shared=("PyTorch",), job_skills=3, meaning=0.6, title_only=0):  # fmt: skip
    """A quick check for the job, and an AI score from `model` when `score` or `failed` is given."""
    conn.execute(
        "INSERT OR REPLACE INTO stage_a (job_id, setup, keywords, job_skills, shared_skills, meaning, title_only, "
        "trick, passed, checked_on) VALUES (?, 'fp', ?, ?, ?, ?, ?, ?, ?, '2026-10-28')",
        (job_id, json.dumps(list(keywords)), job_skills, json.dumps(list(shared)), meaning, title_only, trick, passed),
    )
    if score is not None or failed:
        conn.execute(
            "INSERT OR REPLACE INTO ai_scores (job_id, model, score, reasons, failed, odd, resume, scored_on) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, '2026-10-28T10:00:00+00:00')",
            (job_id, model, score, json.dumps(list(reasons)), failed, odd, resume),
        )


@pytest.fixture
def five(conn):
    """One job in each scoring status, plus a closed one."""
    with conn:
        jobs = {name: add_job(conn, name) for name in ("Ready", "Low", "Quick", "Failed", "Waiting")}
        scored(conn, jobs["Ready"], score=85, reasons=["Strong RL match."])
        scored(conn, jobs["Low"], score=40, reasons=["Different field."], resume="resume-before")
        scored(conn, jobs["Quick"], passed=0)
        scored(conn, jobs["Failed"], failed="AI score failed twice: check this job by hand")
        scored(conn, jobs["Waiting"])
        scored(conn, add_job(conn, "Closed", status="no longer available"), score=90)
    return jobs


def statuses(conn, address="f=1&status=open", scoring=CHOSEN):
    page = queries.list_jobs(conn, queries.Filters.from_address(address), scoring)
    return {job["title"]: job["scoring"] for job in page.jobs}


def test_every_scoring_status(conn, five):
    assert statuses(conn) == {
        "Ready": "ready",
        "Low": "low",
        "Quick": "quick",
        "Failed": "failed",
        "Waiting": "waiting",
    }


def test_the_threshold_decides_ready_at_once(conn, five):
    assert statuses(conn, scoring=queries.Scoring(MODEL, 90))["Ready"] == "low"


def test_prepare_anyway_makes_a_job_ready(conn, five):
    with conn:
        conn.execute("UPDATE jobs SET prepare_anyway = 1 WHERE id = ?", (five["Quick"],))
    assert statuses(conn)["Quick"] == "ready"


def test_another_models_score_is_shown_labelled_but_does_not_decide(conn, five):
    with conn:
        scored(conn, five["Waiting"], score=77, model="qwen3.6:35b-a3b")
    page = queries.list_jobs(conn, queries.Filters.from_address("f=1&status=open&q=Waiting"), CHOSEN)
    assert (page.jobs[0]["scoring"], page.jobs[0]["other"]) == ("waiting", "qwen3.6:35b-a3b: 77")


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("f=1&status=open&scoring=ready", ["Ready"]),
        ("f=1&status=open&scoring=low&scoring=quick", ["Low", "Quick"]),
        ("f=1&status=open&score_from=50", ["Ready"]),
        ("f=1&status=open&score_to=50", ["Low"]),
        ("f=1&status=open&score_from=101", ["Ready", "Low", "Quick", "Failed", "Waiting"]),  # ignored
        ("f=1&status=open&sort=score", ["Ready", "Low", "Waiting", "Failed", "Quick"]),  # unscored jobs last
    ],
)
def test_scoring_filters_and_sort(conn, five, address, expected):
    titles = [job["title"] for job in queries.list_jobs(conn, queries.Filters.from_address(address), CHOSEN).jobs]
    assert titles == expected if "sort" in address else sorted(titles) == sorted(expected)


def test_odd_scores_and_trick_phrases_can_be_filtered(conn, five):
    with conn:
        scored(conn, five["Low"], score=40, odd="a reason copies the job ad word for word")
        scored(conn, five["Quick"], passed=0, trick="Note to AI")
    assert set(statuses(conn, "f=1&status=open&odd=1")) == {"Low", "Quick"}


def test_older_resume_is_marked(conn, five):
    page = queries.list_jobs(conn, queries.Filters.from_address("f=1&status=open"), CHOSEN)
    assert {job["title"] for job in page.jobs if job["older"]} == {"Low"}
    page = queries.list_jobs(conn, queries.Filters.from_address("f=1&status=open"), queries.Scoring(MODEL, 70, None))
    assert not any(job["older"] for job in page.jobs)  # no usable resume: nothing to compare with


def test_counts_add_up_to_every_open_job(conn, five):
    counts = queries.counts(conn, CHOSEN)
    assert counts == {"ready": 1, "low": 1, "quick": 1, "failed": 1, "waiting": 1, "open": 5, "passed": 4}
    assert sum(counts[status] for status in queries.OPTIONS["scoring"]) == counts["open"]


@pytest.fixture
def client(client, monkeypatch):
    monkeypatch.setattr(main, "_scoring", lambda: CHOSEN)
    log_in(client)
    return client


def test_low_score_tab_shows_reasons_and_prepare_anyway(client, conn, five):
    text = client.get(main.LOW_SCORE).text
    assert "<h1>Low score</h1>" in text and "Different field." in text and "Prepare anyway" in text
    assert "Ready" not in text.split("<table")[1]  # only low scores in the list
    reply = client.post(f"/jobs/{five['Low']}/prepare", data={
        "csrf_token": csrf(client), "value": "1", "to": "list", "back": main.LOW_SCORE,
    }, follow_redirects=False)  # fmt: skip
    assert reply.headers["location"] == main.LOW_SCORE
    assert one(conn, "SELECT prepare_anyway FROM jobs WHERE id = ?", five["Low"]) == (1,)


def test_the_jobs_page_shows_counts_and_scores(client, five):
    text = client.get("/").text
    assert "5 open jobs" in text and "1 ready" in text and "2 low score" in text and "4 passed the quick check" in text
    assert "ready for preparation" in text and "85" in text


def test_the_job_page_shows_the_score_reasons_and_quick_check(client, conn, five):
    with conn:
        scored(conn, five["Ready"], score=85, reasons=["Strong RL match."], keywords=["RL"], meaning=0.81,
               shared=["PyTorch", "Python"], job_skills=5, trick="<b>Note to AI</b>")  # fmt: skip
    text = client.get(f"/jobs/{five['Ready']}").text
    for shown in ("ready for preparation", "85 of 100", "Strong RL match.", "Your keywords in the title", "RL",
                  "2 of the job's 5 are on your resume (PyTorch, Python)", "0.81", "passed"):  # fmt: skip
        assert shown in text
    assert "&lt;b&gt;Note to AI&lt;/b&gt;" in text  # text from an ad is shown as text, never as page code


def test_answer_key_marks_can_be_set_changed_and_cleared(client, conn, five):
    job = five["Waiting"]
    for label in ("yes", "no"):
        client.post(f"/jobs/{job}/answer", data={"csrf_token": csrf(client), "label": label})
    assert one(conn, "SELECT label FROM answer_key WHERE job_id = ?", job) == ("no",)
    assert "your answer key: no" in client.get(f"/jobs/{job}").text
    client.post(f"/jobs/{job}/answer", data={"csrf_token": csrf(client), "label": "clear"})
    assert one(conn, "SELECT count(*) FROM answer_key") == (0,)


def test_prepare_anyway_can_be_undone_on_the_job_page(client, conn, five):
    job = five["Low"]
    client.post(f"/jobs/{job}/prepare", data={"csrf_token": csrf(client), "value": "1"})
    assert 'Undo "prepare anyway"' in client.get(f"/jobs/{job}").text
    client.post(f"/jobs/{job}/prepare", data={"csrf_token": csrf(client), "value": "0"})
    assert one(conn, "SELECT prepare_anyway FROM jobs WHERE id = ?", job) == (0,)


@pytest.mark.parametrize(
    ("path", "data", "status"),
    [
        ("/jobs/999999/answer", {"label": "yes"}, 404),
        ("/jobs/999999/prepare", {"value": "1"}, 404),
        ("/jobs/{job}/answer", {"label": "perhaps"}, 400),
    ],
)
def test_unknown_jobs_and_labels_are_refused(client, five, path, data, status):
    reply = client.post(path.format(job=five["Low"]), data={"csrf_token": csrf(client)} | data)
    assert reply.status_code == status


def test_a_changed_ad_and_a_failed_new_try_are_marked(client, conn, five):
    failed = "AI score failed twice (no answer within 120 seconds; then no answer within 120 seconds)"
    with conn:
        conn.execute("UPDATE ai_scores SET changed = 1 WHERE job_id = ?", (five["Low"],))
        conn.execute(
            "INSERT INTO ai_failures (job_id, model, failed, resume, tried_on) VALUES (?, ?, ?, 'resume-now', "
            "'2026-10-29T09:00:00+00:00')",
            (five["Low"], MODEL, failed),
        )
    page = queries.list_jobs(conn, queries.Filters.from_address("f=1&status=open"), CHOSEN)
    marked = {job["title"]: (job["ad_changed"], job["new_try_failed"]) for job in page.jobs if job["score"]}
    assert marked == {"Low": (1, failed), "Ready": (0, None)}
    assert "the ad changed since" in client.get("/").text
    text = client.get(f"/jobs/{five['Low']}").text
    assert "40 of 100" in text and "Different field." in text  # the older score and its reasons stay
    assert f"A new try on 2026-10-29 failed: {failed}." in text and "the ad changed since" in text
