"""What the Writer made, on the dashboard: the tailored PDF, building it again, the changes page, the Ready page's
statuses and the Usage page. A made-up person and job; versions written straight into the test database."""

import json

import pytest
from helpers import add_job, csrf, log_in, one
from test_privacy import CONTACT
from test_tiles import RESUME_CLS
from test_web_scoring import CHOSEN, scored
from test_writer import PROFILE

from app import config, questionnaire
from app.documents import pdf, tile_sets
from app.resume import RESUME_FILE
from app.web import drafts, main, prepare, queries

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
    path.write_text(
        f"data_dir: {data_dir}\nai:\n"
        + "".join(
            f"  {member}:\n    model: gemma4:26b-a4b-it-q8_0\n"
            for member in ("writer", "truth", "hiring", "editor", "ats", "domain")
        ),
        encoding="utf-8",
    )
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
    client.started, client.waived = [], []

    def start(conn, config, job, kind="resume", feedback="", waived=frozenset(), mode="half", automatic=False,
              writer_only=False):  # fmt: skip
        client.started.append((job, feedback or kind) if feedback or kind != "resume" else job)
        client.waived.append(waived)

    monkeypatch.setattr(prepare.flow, "start", start)
    monkeypatch.setattr(client.app.state.preparer, "wake", lambda: None)
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
    version(conn, job, 2, status="stopped")  # no draft
    assert client.get(f"/jobs/{job}/changes?version=1").status_code == 200  # that version, not the newest
    for address in (f"/jobs/{job}/changes?version=2", f"/jobs/{job}/changes?version=3", f"/jobs/{job}/changes",
                    f"/jobs/{job}/changes?version=x", f"/jobs/{job + 1}/changes?version=1"):  # fmt: skip
        assert client.get(address).status_code == 404


def test_the_box_and_the_ready_page_check_a_version_without_loading_your_entries(client, conn, job, monkeypatch):
    version(conn, job)
    loads, scores, score = [], [], queries.job_scoring
    monkeypatch.setattr(tile_sets, "tiles", lambda *found: loads.append(found) or [])
    monkeypatch.setattr(queries, "job_scoring", lambda *found: scores.append(found) or score(*found))
    assert "Done: version 1" in client.get(f"/jobs/{job}/preparation").text
    assert len(scores) == 1  # the job's score, read once
    assert "Writer only (no council): version 1" in client.get("/ready").text  # a version from before the council
    assert loads == []  # only each set's number was read


def test_the_changes_page_puts_back_what_each_placeholder_stood_for(client, conn, job):
    version(conn, job)
    with conn:
        conn.execute(
            "UPDATE preparations SET draft = ?", (json.dumps(DRAFT | {"details": {"[LINK]": "github.com/x"}}),)
        )
    questionnaire.save(conn, questionnaire.check({"contact": CONTACT | {"linkedin": "https://linkedin.com/in/x"}}))
    assert "Shared the &lt;b&gt;code&lt;/b&gt; at github.com/x." in client.get(f"/jobs/{job}/changes?version=1").text


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


def test_building_the_pdf_again_says_when_a_bullet_shows_a_detail_changed_since(client, conn, job, monkeypatch):
    version(conn, job)
    with conn:
        conn.execute(
            "UPDATE preparations SET draft = ?", (json.dumps(DRAFT | {"details": {"[LINK]": "github.com/x"}}),)
        )
    monkeypatch.setattr(drafts.pdf, "build_kept", lambda data_dir, target, source: None)
    client.post(f"/jobs/{job}/pdf", data={"csrf_token": csrf(client)})
    assert (
        "PDF built, but a bullet still shows a link you have changed since"
        in client.get(f"/jobs/{job}/preparation").text
    )


def test_the_box_shows_the_pdf_its_pages_and_every_version(client, conn, data_dir, job):
    version(conn, job, 1, "by_hand", "Writer failed twice (x): check by hand, or press Retry.", cost=0.5)
    version(conn, job, 2)
    fake_pdf(data_dir, job, 2, pages=2, missing=["☺"])
    text = client.get(f"/jobs/{job}/preparation").text
    assert (
        "Done: version 2, written 2026-10-30 · Writer only (no council)." in text
        and f'href="/documents/{job}/resume-v2.pdf"' in text
    )
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
    assert "Writer only (no council): version 2" in text and f'href="/jobs/{job}/changes?version=2">Changes' in text
    assert "2 pages</span>" in text and "$0.25" in text and ">Prepare</button>" not in text
    version(conn, job, 3, problem="PDF built, but a bullet still shows a link you have changed since: update it.")
    assert "PDF built, but a bullet still shows a link you have changed since: see the job" in client.get("/ready").text


def test_the_usage_page_shows_each_period_and_todays_spend(client, conn, job, monkeypatch):
    assert "No AI calls yet" in client.get("/usage").text
    version(conn, job, cost=1.5)
    monkeypatch.setattr(drafts.spending, "today", lambda: "2026-10-30")
    text = client.get("/usage").text
    assert "Today: $1.50 of your $15.00 daily limit." in text and "This month: $1.50 of $150.00." in text
    assert "<td>2026-10-30</td><td>0</td><td>1</td>" in text and 'aria-current="page" href="/usage?by=day"' in text
    assert "<td>2026-10-26</td>" in client.get("/usage?by=week").text  # the Monday of that week
    assert "<td>2026-01-01</td>" in client.get("/usage?by=year").text
    assert 'aria-current="page" href="/usage?by=day"' in client.get("/usage?by=century").text
    assert 'href="/usage"' in text  # in the menu


TRY = """INSERT INTO failed_tries (step_id, try_number, output_tokens, cost, reason, reply, reply_length, saved_on)
    SELECT id, ?, 8192, ?, ?, ?, ?, '2026-10-30T10:05:00+00:00' FROM steps WHERE preparation_id = ?"""


def test_the_failed_tries_page_shows_what_came_back(client, conn, job):
    assert "No failed tries" in client.get("/failed-tries").text and 'href="/failed-tries"' in client.get("/usage").text
    found = version(conn, job, 1, "by_hand", "Writer failed twice (x): check by hand, or press Retry.")
    with conn:
        conn.execute(TRY, (1, 0.17, "the answer was cut off", '{"entries": [{"text": "<b>[ME]</b>', 120_000, found))
        conn.execute(TRY, (2, None, "no answer within 600 seconds", None, None, found))
    text = client.get("/failed-tries").text
    assert text.index("no answer within 600 seconds") < text.index("the answer was cut off")  # newest first
    assert "&lt;b&gt;[ME]&lt;/b&gt;" in text and "<b>[ME]" not in text  # as it came, never run as a page
    assert "(the first 34 of 120,000 characters)" in text and "Nothing came back." in text and "not counted" in text
    assert "2026-10-30 10:05" in text and "Resume 1" in text and f'href="/jobs/{job}"' in text and "$0.17" in text
    assert f'href="/failed-tries?version={found}">2 failed tries</a>' in client.get(f"/jobs/{job}/preparation").text
    assert "cut off" in client.get(f"/failed-tries?version={found}").text
    assert "No failed tries for this version" in client.get(f"/failed-tries?version={found + 1}").text


def test_a_cover_letter_with_failed_tries_links_to_them(client, conn, job):
    letter_version(conn, job, 1, "by_hand", "Writer failed twice.")
    (letter,) = conn.execute("SELECT id FROM preparations WHERE kind = 'cover_letter'").fetchone()
    with conn:
        conn.execute(STEP, (letter, 0))
        conn.execute(TRY, (1, 0, "reason", None, None, letter))
    link = f'<a href="/failed-tries?version={letter}">1 failed try</a>'
    assert f"Cover letter 1 · {link}" in client.get(f"/jobs/{job}/preparation").text
    letter_version(conn, job, 2)  # the newest has none; the earlier one still links
    text = client.get(f"/jobs/{job}/preparation").text
    assert f"($0.00 · {link})" in text and "Cover letter 2 ·" not in text


def test_the_failed_tries_page_goes_page_by_page(client, conn, job, monkeypatch):
    monkeypatch.setattr(drafts.queries, "PAGE_SIZE", 1)
    found = version(conn, job, 1, "by_hand", "Writer failed twice.")
    with conn:
        for number in (1, 2):
            conn.execute(TRY, (number, 0, f"reason {number}", None, None, found))
    first = client.get("/failed-tries").text
    assert "reason 2" in first and "reason 1" not in first and 'href="/failed-tries?page=2">Next' in first
    second = client.get("/failed-tries?page=2").text
    assert "reason 1" in second and "Next" not in second and 'href="/failed-tries?page=1">Previous' in second
    for odd in ("9" * 5000, "²", "-1", "0"):  # never a crash
        assert client.get(f"/failed-tries?page={odd}&version={odd}").status_code == 200


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


def test_request_changes_while_writing_is_kept_for_the_next_round_and_needs_current_entries(client, conn, job):
    version(conn, job)
    writing = version(conn, job, number=2, status="writing")
    ask(client, job, "Shorter.")
    asked = json.loads(one(conn, "SELECT asked FROM preparations WHERE id = ?", writing)[0])
    assert [item["words"] for item in asked] == ["Shorter."] and client.started == []  # added, not a new version
    with conn:
        conn.execute("UPDATE preparations SET status = 'stopped' WHERE id = ?", (writing,))
    tile_sets.confirm(conn, PROFILE[:2], CHOSEN.resume)  # your entries changed
    assert "Your entries changed since version 1" in ask(client, job, "Shorter.") and client.started == []


def test_request_changes_on_a_skipped_job_asks_first(client, conn, job):
    version(conn, job)
    with conn:
        conn.execute("UPDATE jobs SET not_interested = 1 WHERE id = ?", (job,))
    text = client.get(f"/jobs/{job}/preparation").text
    assert "Request changes to version 1" in text and 'hx-confirm="This job is skipped' in text
    assert "This job is skipped" in ask(client, job, "Shorter.") and client.started == []  # a page that didn't ask
    client.post(f"/jobs/{job}/request-changes", data={"csrf_token": csrf(client), "feedback": "Shorter.",
                                                      "anyway": "skipped"})  # fmt: skip
    assert client.started == [(job, "Shorter.")] and client.waived == [{"skipped"}]
    assert client.post("/jobs/999/request-changes", data={"csrf_token": csrf(client)}).status_code == 404


def test_retry_after_a_failed_change_sends_the_same_words(client, conn, job):
    version(conn, job)
    with conn:
        failed = version(conn, job, 2, "by_hand", "Writer failed twice (x): check by hand, or press Retry.")
        conn.execute("UPDATE preparations SET feedback = 'Lead with the lab.' WHERE id = ?", (failed,))
    text = client.get(f"/jobs/{job}/preparation").text
    assert "you asked: Lead with the lab." in text and ">Retry</button>" in text
    assert "Request changes to version 1" in text  # built on the newest finished version
    client.post(f"/ready/{job}/prepare", data={"csrf_token": csrf(client)})
    assert client.started == [(job, "Lead with the lab.")]


LETTER = {"paragraphs": ["I want this job.", "I trained arms to 95% success.", "Thanks."], "from": [], "notes": ""}
LETTER_VERSION = """INSERT INTO preparations (job_id, kind, version, sources, writer, status, problem, draft, flags,
    created_on) VALUES (?, 'cover_letter', ?, '[]', '{}', ?, ?, ?, ?, '2026-10-30T10:00:00+00:00')"""


def letter_version(conn, job, number=1, status="done", problem=None):
    with conn:
        draft = json.dumps(LETTER) if status == "done" else None
        flags = json.dumps([{"paragraph": 1, "numbers": ["95"]}])
        conn.execute(LETTER_VERSION, (job, number, status, problem, draft, flags))


def test_write_cover_letter_starts_the_writer_and_the_box_shows_the_letter(client, conn, data_dir, job):
    text = client.get(f"/jobs/{job}/preparation").text
    assert ">Write cover letter</button>" in text
    text = client.post(f"/jobs/{job}/cover-letter", data={"csrf_token": csrf(client)}).text
    assert client.started == [(job, "cover_letter")] and 'id="preparation"' in text
    letter_version(conn, job)
    target = pdf.tailored(data_dir, job, 1, "cover_letter")
    target.parent.mkdir(parents=True)
    target.write_bytes(b"%PDF-1.7 letter")
    target.with_suffix(".json").write_text('{"pages": 1, "missing": []}', encoding="utf-8")
    text = client.get(f"/jobs/{job}/preparation").text
    assert (
        "<p>I trained arms to <mark>95</mark>% success.</p>" in text and ">Write another cover letter</button>" in text
    )
    assert f'href="/documents/{job}/cover-letter-v1.pdf"' in text and "(1 page)" in text
    assert client.get(f"/documents/{job}/cover-letter-v1.pdf").content == b"%PDF-1.7 letter"
    assert client.get(f"/documents/{job}/cover-letter-v2.pdf").status_code == 404


def test_a_letter_needs_the_same_as_prepare_and_waits_for_the_writer(client, conn, data_dir, job, monkeypatch):
    letter_version(conn, job, status="writing")
    text = client.post(f"/jobs/{job}/cover-letter", data={"csrf_token": csrf(client)}).text
    assert "The Writer is still working on this job" in text and client.started == []
    assert ">Write cover letter</button>" not in client.get(f"/jobs/{job}/preparation").text
    with conn:
        conn.execute("DELETE FROM preparations WHERE kind = 'cover_letter'")
    with conn:
        conn.execute("UPDATE jobs SET not_interested = 1 WHERE id = ?", (job,))
    assert "This job is skipped" in client.post(f"/jobs/{job}/cover-letter", data={"csrf_token": csrf(client)}).text
    client.post(f"/jobs/{job}/cover-letter", data={"csrf_token": csrf(client), "anyway": "skipped"})  # the pop-up's OK
    assert client.started == [(job, "cover_letter")] and client.waived == [{"skipped"}]
    assert client.post("/jobs/999/cover-letter", data={"csrf_token": csrf(client)}).status_code == 404
    assert ">Write cover letter</button>" in client.get(f"/jobs/{job}/preparation").text  # the pop-up asks first
    tile_sets.confirm(conn, PROFILE, "another-resume")  # entries no longer confirmed for your current resume
    assert ">Write cover letter</button>" not in client.get(f"/jobs/{job}/preparation").text
    tile_sets.confirm(conn, PROFILE, CHOSEN.resume)
    config.CONFIG_PATH.write_text(f"data_dir: {data_dir}\n", encoding="utf-8")  # no Writer chosen
    assert ">Write cover letter</button>" not in client.get(f"/jobs/{job}/preparation").text


def test_the_letters_pdf_can_be_built_again(client, conn, data_dir, job, monkeypatch):
    letter_version(conn, job, problem="PDF could not be built: The PDF build took over 5 minutes and was stopped.")
    built = []
    monkeypatch.setattr(drafts.pdf, "build_kept", lambda data_dir, target, source: built.append((target, source)))
    client.post(f"/jobs/{job}/pdf", data={"csrf_token": csrf(client), "kind": "cover_letter"})
    ((target, source),) = built
    assert target == pdf.tailored(data_dir, job, 1, "cover_letter") and "I trained arms to 95\\% success." in source
    assert "PDF could not be built" not in client.get(f"/jobs/{job}/preparation").text


COUNCIL = json.dumps({"members": {m: {"model": "gemma4:26b-a4b-it-q8_0"} for m in ("hiring", "truth")},
                      "names": {}})  # fmt: skip
ANSWER = """INSERT INTO steps (preparation_id, round, member, turn, service, model, mode, status, answer, retry_after,
    day, sent_on) VALUES (?, ?, ?, ?, 'ollama', 'm', ?, ?, ?, ?, 'd', 'now')"""


def council_version(conn, job, status="done", agreed=None, round_=1, problem=None, draft=DRAFT):
    with conn:
        found = version(conn, job, status=status, problem=problem)
        conn.execute("DELETE FROM steps WHERE preparation_id = ?", (found,))  # each test adds the steps it needs
        conn.execute("UPDATE preparations SET council = ?, agreed = ?, round = ?, draft = ? WHERE id = ?",
                     (COUNCIL, agreed, round_, json.dumps(draft) if status == "done" else None, found))  # fmt: skip
    return found


def review(objections=(), votes=(), renumbered=()):
    return json.dumps({"verdict": "object" if objections else "approve", "objections": list(objections),
                       "votes": list(votes), "summary": "", "renumbered": list(renumbered)})  # fmt: skip


def test_each_version_says_where_its_council_is(conn, job):
    rounds = 3
    found = council_version(conn, job, status="writing", round_=2)
    latest = drafts.runs.latest(conn, job)
    assert drafts.status(conn, latest, rounds) == "council round 2 of 3"
    with conn:
        conn.execute(ANSWER, (found, 2, "truth", 0, "batch", "sent", None, None))
    assert drafts.status(conn, latest, rounds) == "waiting for Anthropic"
    with conn:
        conn.execute("DELETE FROM steps WHERE member = 'truth'")
        conn.execute(ANSWER, (found, 2, "hiring", 0, "flex", "waiting", None, "2026-11-02T03:20:00+00:00"))
    assert drafts.status(conn, latest, rounds).startswith("waiting: busy, again at ")
    latest["problem"] = "Paused: today's spending limit of $15.00 would be passed ($14.99 spent today)."
    assert drafts.status(conn, latest, rounds) == "Paused: today's spending limit of $15.00 would be passed"
    for agreed, line in ((1, "council agreed"), (0, "no agreement: see the job"), (None, "Writer only (no council)")):
        assert drafts.status(conn, latest | {"status": "done", "agreed": agreed}, rounds) == line


def test_the_box_shows_the_councils_notes_truth_checker_first(client, conn, job):
    found = council_version(conn, job, agreed=0)
    must = {"id": "TC-1", "where": "E2.1", "severity": "must", "problem": "Says 95%.", "fix": "Say 90%.",
            "quote": "reaching 90% success"}  # fmt: skip
    with conn:
        conn.execute(ANSWER, (found, 1, "hiring", 0, "direct", "done", review(
            votes=[{"objection": "TC-1", "agree": True, "why": "Right."}],
            renumbered=[{"wrote": "TC-1", "now": "agree"}]), None))  # fmt: skip
        conn.execute(ANSWER, (found, 1, "truth", 0, "direct", "done", review([must]), None))
        conn.execute(ANSWER, (found, 1, "writer", 0, "direct", "done", json.dumps({"draft": {
            "responses": [{"objection": "HM-1", "action": "fixed", "why": "Shorter."}],
            "missed": [{"objection": "TC-1", "severity": "must"}]}, "flags": []}), None))  # fmt: skip
    text = client.get(f"/jobs/{job}/preparation").text
    assert text.index("<strong>Truth checker</strong>") < text.index("<strong>Hiring manager</strong>")  # first
    assert 'class="error">TC-1 (must, E2.1): Says 95%.' in text and "Your original:</em> “reaching 90% success”" in text
    assert "Hiring manager agrees: Right." in text and "HM-1 fixed (Shorter.)" in text
    assert "No reply from the Writer: TC-1 (must)" in text and "the app renumbered: TC-1 → agree" in text


def test_no_agreement_shows_the_objected_entries_and_the_wording_switch(client, conn, job, monkeypatch):
    found = council_version(conn, job, agreed=0, draft=DRAFT | {"objected": [["profile", "exp-1"]]})
    with conn:
        conn.execute("UPDATE preparations SET wording = 'original' WHERE id = ?", (found,))
    built = []
    monkeypatch.setattr(main.council_view.pdf, "build_kept", lambda data_dir, target, source: built.append(source))
    text = client.get(f"/jobs/{job}/preparation").text
    assert "The Truth checker still objects to: Demo Robotics Lab." in text and "your original wording there" in text
    assert ">Keep the Writer&#39;s wording</button>" in text
    assert "In the PDF: your original wording" in client.get(f"/jobs/{job}/changes?version=1").text
    client.post(f"/jobs/{job}/wording", data={"csrf_token": csrf(client), "wording": "writer"})
    assert one(conn, "SELECT wording FROM preparations")[0] == "writer" and len(built) == 1  # PDF built again
    assert ">Use my original wording</button>" in client.get(f"/jobs/{job}/preparation").text
    assert client.post(f"/jobs/{job}/wording", data={"csrf_token": csrf(client), "wording": "mine"}).status_code == 404


def test_now_normal_price_switches_a_running_version_or_starts_one(client, conn, job):
    found = council_version(conn, job, status="writing")
    text = client.get(f"/jobs/{job}/preparation").text
    assert "Now, normal price (about $0.00)" in text and "It counts in your limits." in text  # laptop: free
    client.post(f"/jobs/{job}/normal-price", data={"csrf_token": csrf(client)})
    assert one(conn, "SELECT mode FROM preparations WHERE id = ?", found)[0] == "normal" and client.started == []
    with conn:
        conn.execute("UPDATE preparations SET status = 'stopped' WHERE id = ?", (found,))
    client.post(f"/jobs/{job}/normal-price", data={"csrf_token": csrf(client)})
    assert client.started == [job]  # a new version, at the normal price


def test_continue_anyway_carries_on_a_version_stopped_for_a_closed_job(client, conn, job):
    from app.council import flow

    found = council_version(conn, job, status="stopped", problem=flow.CLOSED)
    assert ">Continue anyway</button>" in client.get(f"/jobs/{job}/preparation").text
    letter_version(conn, job, status="writing")  # another version being written: not now
    assert client.post(f"/jobs/{job}/continue", data={"csrf_token": csrf(client)}).status_code == 404
    with conn:
        conn.execute("DELETE FROM preparations WHERE kind = 'cover_letter'")
    client.post(f"/jobs/{job}/continue", data={"csrf_token": csrf(client)})
    assert one(conn, "SELECT status, waived, problem FROM preparations WHERE id = ?", found) == (
        "writing", '["closed"]', None,
    )  # fmt: skip
    assert client.post(f"/jobs/{job}/continue", data={"csrf_token": csrf(client)}).status_code == 404  # writing now


def test_request_changes_for_a_cover_letter(client, conn, job):
    letter_version(conn, job)
    text = client.get(f"/jobs/{job}/preparation").text
    assert "Request changes to cover letter 1" in text
    form = {"csrf_token": csrf(client), "feedback": "Warmer."}
    client.post(f"/jobs/{job}/letter-changes", data=form)
    assert client.started == [(job, "Warmer.")]
    with conn:
        conn.execute("UPDATE preparations SET status = 'writing', draft = NULL WHERE kind = 'cover_letter'")
    client.post(f"/jobs/{job}/letter-changes", data=form | {"feedback": "Shorter."})
    asked = json.loads(one(conn, "SELECT asked FROM preparations WHERE kind = 'cover_letter'")[0])
    assert [item["words"] for item in asked] == ["Shorter."] and len(client.started) == 1
    assert "Type what you&#39;d like changed" in client.post(f"/jobs/{job}/letter-changes",
                                                             data=form | {"feedback": " "}).text  # fmt: skip


def test_the_top_bar_shows_the_line_up_spend_and_a_pause(client, conn, job, data_dir):
    text = client.get("/ready").text
    assert "Line-up: Custom (up to $0.00 per job)" in text and "Today $0.00 of $15.00" in text
    assert "Scoring: laptop, free" in text and "Paused" not in text and "<td>Truth checker</td>" in text
    version(conn, job)
    with conn:
        conn.execute("UPDATE steps SET cost = 15, day = ?", (drafts.spending.today(),))
    assert "Paused: today&#39;s limit reached" in client.get("/ready").text
