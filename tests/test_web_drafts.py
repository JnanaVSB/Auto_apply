"""What the Writer made, on the dashboard: the tailored PDF, building it again, the changes page, the Ready page's
statuses and the Usage page. A made-up person and job; versions written straight into the test database."""

import json

import pytest
from helpers import add_job, csrf, log_in
from test_privacy import CONTACT
from test_tiles import RESUME_CLS
from test_web_scoring import CHOSEN, scored
from test_writer import PROFILE

from app import config, questionnaire
from app.documents import pdf, tile_sets
from app.resume import RESUME_FILE
from app.web import drafts, main

DRAFT = {
    "entries": [
        {"source": "profile", "tile_id": "exp-1", "skills": [], "bullets": [
            {"text": "Raised a robot arm's success from 90% to 95% with PPO.", "from": [0]},
            {"text": "Shared the <b>code</b> at [LINK].", "from": [2]}]},
        {"source": "profile", "tile_id": "skills-1", "bullets": [], "skills": ["Rust", "Python"]},
        {"source": "profile", "tile_id": "pub-1", "skills": [], "bullets": [{"text": "[ME]", "from": [0]}]},
    ],
    "notes": "Put the robot learning first.",
}  # fmt: skip
FLAGS = [{"source": "profile", "tile_id": "exp-1", "bullet": 0, "numbers": ["95"]}]
WRITER = '{"model": "gemma4:26b-a4b-it-q8_0"}'
VERSION = """INSERT INTO preparations (job_id, kind, version, sources, writer, status, problem, draft, flags,
    created_on) VALUES (?, 'resume', ?, '[{"source": "profile", "set": 1}]', ?, ?, ?, ?, ?,
    '2026-10-30T10:00:00+00:00')"""
STEP = """INSERT INTO steps (preparation_id, service, model, status, cost, day, sent_on)
    VALUES (?, 'anthropic', 'claude-opus-5-5', 'done', ?, '2026-10-30', 'now')"""


@pytest.fixture
def job(conn, data_dir, tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(f"data_dir: {data_dir}\nai:\n  writer:\n    model: gemma4:26b-a4b-it-q8_0\n", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    (data_dir / RESUME_FILE).write_text(RESUME_CLS, encoding="utf-8")
    questionnaire.save(conn, questionnaire.check({"contact": CONTACT}))
    tile_sets.confirm(conn, PROFILE, CHOSEN.resume)
    with conn:
        found = add_job(conn, "Robot Learning Intern")
        scored(conn, found, score=85)
    return found


@pytest.fixture
def client(client, monkeypatch):
    monkeypatch.setattr(main, "_scoring", lambda: CHOSEN)
    client.started = []
    monkeypatch.setattr(client.app.state.preparer, "start", lambda job, words="": client.started.append(
        (job, words) if words else job))  # fmt: skip
    log_in(client)
    return client


def version(conn, job, number=1, status="done", problem=None, cost=0.25):
    with conn:
        draft = json.dumps(DRAFT) if status == "done" else None
        found = conn.execute(VERSION, (job, number, WRITER, status, problem, draft, json.dumps(FLAGS))).lastrowid
        conn.execute(STEP, (found, cost))
    return found


def fake_pdf(data_dir, job, number=1, pages=1, missing=()):
    target = pdf.tailored(data_dir, job, number)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"%PDF-1.7 made-up")
    target.with_suffix(".json").write_text(json.dumps({"pages": pages, "missing": list(missing)}), encoding="utf-8")


def test_the_tailored_pdf_is_served_only_by_job_and_version_number(client, conn, data_dir, job):
    assert client.get(f"/documents/{job}/resume-v1.pdf").status_code == 404
    fake_pdf(data_dir, job)
    answer = client.get(f"/documents/{job}/resume-v1.pdf")
    assert answer.content == b"%PDF-1.7 made-up" and answer.headers["content-type"] == "application/pdf"
    for address in (f"/documents/{job}/resume-v1.json", f"/documents/{job}/resume-v..%2F..%2Fapp.db.pdf",
                    "/documents/x/resume-v1.pdf", f"/documents/{job}/cover-v1.pdf"):  # fmt: skip
        assert client.get(address).status_code == 404


def test_the_changes_page_shows_each_entry_beside_the_writers_version(client, conn, job):
    version(conn, job)
    text = " ".join(client.get(f"/jobs/{job}/changes?version=1").text.split())  # as the browser shows it
    assert "version 1, written 2026-10-30 by gemma4:26b-a4b-it-q8_0" in text and "Put the robot learning first." in text
    assert "<li>Trained a pretend robot arm with PPO, reaching 90% success.</li>" in text  # the original
    assert "from 90% to <mark>95</mark>% with PPO." in text  # only the new number marked
    assert "Shared the &lt;b&gt;code&lt;/b&gt; at https://github.com/alex-example." in text  # escaped, details back
    assert "<p>Rust, Python</p>" in text and "left out: C++ (17, 20)" in text  # the skills chosen and left out
    assert text.count("left out</span>") == 3  # the summary, the drone project, the school
    assert "kept as written" in text and "[ME]" not in text and "(from 0)" in text and "(from 2)" in text
    for address in (f"/jobs/{job}/changes?version=2", f"/jobs/{job}/changes?version=x", f"/jobs/{job}/changes",
                    f"/jobs/{job + 1}/changes?version=1"):  # fmt: skip
        assert client.get(address).status_code == 404


def test_without_your_name_the_changes_page_shows_the_placeholders(client, conn, job):
    questionnaire.save(conn, questionnaire.check({}))
    version(conn, job)
    assert "Shared the &lt;b&gt;code&lt;/b&gt; at [LINK]." in client.get(f"/jobs/{job}/changes?version=1").text


def test_building_the_pdf_again_notes_how_it_went(client, conn, data_dir, job, monkeypatch):
    assert client.post(f"/jobs/{job}/pdf", data={"csrf_token": csrf(client)}).status_code == 404  # nothing done yet
    version(conn, job, 1, "by_hand", "Writer failed twice (x): check by hand, or press Retry.")
    assert client.post(f"/jobs/{job}/pdf", data={"csrf_token": csrf(client)}).status_code == 404  # no draft to build
    found = version(conn, job, 2, problem="PDF could not be built: The PDF build took over 5 minutes and was stopped.")
    built = []
    monkeypatch.setattr(drafts.pdf, "build_kept", lambda data_dir, target, source: built.append(target))
    text = client.post(f"/jobs/{job}/pdf", data={"csrf_token": csrf(client)}).text
    assert built == [pdf.tailored(data_dir, job, 2)] and "PDF could not be built" not in text
    assert conn.execute("SELECT problem FROM preparations WHERE id = ?", (found,)).fetchone() == (None,)

    def failing(data_dir, target, source):
        raise pdf.PdfError(pdf.NO_NAME)

    monkeypatch.setattr(drafts.pdf, "build_kept", failing)
    text = client.post(f"/jobs/{job}/pdf", data={"csrf_token": csrf(client)}).text
    assert (
        "PDF could not be built: Fill in the Questionnaire&#39;s Contact section first"
        in client.get(f"/jobs/{job}/preparation").text
        and ">Build the PDF again</button>" in client.get(f"/jobs/{job}/preparation").text
    )


def test_the_box_shows_the_pdf_its_pages_and_every_version(client, conn, data_dir, job):
    version(conn, job, 1, "by_hand", "Writer failed twice (x): check by hand, or press Retry.", cost=0.5)
    version(conn, job, 2)
    fake_pdf(data_dir, job, 2, pages=2, missing=["☺"])
    text = client.get(f"/jobs/{job}/preparation").text
    assert "Done: version 2, written 2026-10-30." in text and f'href="/documents/{job}/resume-v2.pdf"' in text
    assert "(2 pages)" in text and "Over one page (2 pages)" in text and "Left out of the PDF" in text
    assert "<td>check by hand</td>" in text and "$0.50" in text and "$0.25" in text
    assert f'href="/jobs/{job}/changes?version=2">2</a>' in text and "Build the PDF again" not in text


def test_the_ready_page_shows_each_jobs_state_cost_and_buttons(client, conn, data_dir, job):
    text = client.get("/ready").text
    assert "not started" in text and ">Prepare</button>" in text and "$0.00" in text
    version(conn, job, 1, "stopped", "Paused: today's spending limit of $15.00 would be passed.", cost=0)
    assert "stopped</span>" in client.get("/ready").text and ">Retry</button>" in client.get("/ready").text
    version(conn, job, 2, cost=0.25)
    fake_pdf(data_dir, job, 2, pages=2)
    text = client.get("/ready").text
    assert "done: version 2" in text and f'href="/jobs/{job}/changes?version=2">Changes' in text
    assert "2 pages</span>" in text and "$0.25" in text and ">Prepare</button>" not in text


def test_the_usage_page_shows_each_period_and_todays_spend(client, conn, job, monkeypatch):
    assert "No AI calls yet" in client.get("/usage").text
    version(conn, job, cost=1.5)
    monkeypatch.setattr(drafts.spending, "today", lambda: "2026-10-30")
    text = client.get("/usage").text
    assert "Today: $1.50 of your $15.00 daily limit." in text and "This month: $1.50 of $150.00." in text
    assert "<td>2026-10-30</td><td>1</td>" in text and 'aria-current="page" href="/usage?by=day"' in text
    assert "<td>2026-10-26</td>" in client.get("/usage?by=week").text  # the Monday of that week
    assert "<td>2026-01-01</td>" in client.get("/usage?by=year").text
    assert 'aria-current="page" href="/usage?by=day"' in client.get("/usage?by=century").text
    assert 'href="/usage"' in text  # in the menu


def ask(client, job, words):
    return client.post(f"/jobs/{job}/request-changes", data={"csrf_token": csrf(client), "feedback": words}).text


def test_request_changes_sends_your_words_to_the_writer(client, conn, job):
    assert "Request changes to version" not in client.get(f"/jobs/{job}/preparation").text  # nothing to change yet
    assert "There is no finished version to change yet" in ask(client, job, "Shorter.") and client.started == []
    version(conn, job)
    assert "Request changes to version 1" in client.get(f"/jobs/{job}/preparation").text
    assert f'href="/jobs/{job}#preparation">Request changes' in client.get("/ready").text
    text = ask(client, job, "  Lead with   the lab.\x07 ")
    assert client.started == [(job, "Lead with the lab.")] and 'id="preparation"' in text  # the box comes back


@pytest.mark.parametrize(
    ("words", "problem"),
    [("", "Type what you&#39;d like changed"), ("x" * 2001, "at most 2,000 characters")],
)
def test_request_changes_needs_your_words(client, conn, job, words, problem):
    version(conn, job)
    text = ask(client, job, words)
    assert problem in text and client.started == []


def test_request_changes_waits_for_the_writer_and_current_entries(client, conn, job, monkeypatch):
    version(conn, job)
    monkeypatch.setattr(client.app.state.preparer, "place", lambda job_id: 1)
    assert "The Writer is still working on this job" in ask(client, job, "Shorter.")
    assert "Request changes to version" not in client.get(f"/jobs/{job}/preparation").text
    monkeypatch.setattr(client.app.state.preparer, "place", lambda job_id: None)
    tile_sets.confirm(conn, PROFILE[:2], CHOSEN.resume)  # your entries changed
    assert "Your entries changed since version 1" in ask(client, job, "Shorter.") and client.started == []


def test_retry_after_a_failed_change_sends_the_same_words(client, conn, job):
    version(conn, job)
    with conn:
        failed = version(conn, job, 2, "by_hand", "Writer failed twice (x): check by hand, or press Retry.")
        conn.execute("UPDATE preparations SET feedback = 'Lead with the lab.' WHERE id = ?", (failed,))
    text = client.get(f"/jobs/{job}/preparation").text
    assert "you asked: Lead with the lab." in text and ">Retry</button>" in text
    client.post(f"/ready/{job}/prepare", data={"csrf_token": csrf(client)})
    assert client.started == [(job, "Lead with the lab.")]
