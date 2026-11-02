"""The pilot, with made-up jobs, a made-up person and fake models (nothing here reaches a real AI)."""

import json
from datetime import UTC, datetime

import pytest
from helpers import add_job, csrf, log_in, one
from test_flow import GOOD, made_up_job
from test_reviewers import QUOTE, review
from test_runs import LOCAL
from test_web_ready import scored

from app import config as settings
from app.config import AISettings, Config, CouncilSettings
from app.council import flow, pilot, runner
from app.documents import privacy
from app.resume import from_latex
from app.web.council_settings import MEMBERS, PRESETS

LAPTOP = dict(zip(MEMBERS, PRESETS["laptop"], strict=True))
NOW = datetime(2026, 11, 2, 23, tzinfo=UTC)


@pytest.fixture
def pdfs(monkeypatch):
    found = []
    monkeypatch.setattr(flow.pdf, "build_kept", lambda data_dir, target, source: found.append(target))
    return found


@pytest.fixture
def jobs(conn, data_dir):
    """Six ready jobs at four companies (posted on different days) beside the made-up Profile; newest first."""
    first = made_up_job(conn, data_dir)
    resume = from_latex((data_dir / "resume.tex").read_text(encoding="utf-8")).fingerprint
    with conn:
        found = [add_job(conn, f"Job {n}", company=company, date_posted=f"2026-10-0{n}")
                 for n, company in enumerate(("Acme", "Acme", "Beta", "Gamma", "Delta", "Beta"), 1)]  # fmt: skip
        for job in found:
            scored(conn, job, score=90, resume=resume)
        conn.execute("UPDATE jobs SET status = 'closed' WHERE id = ?", (first,))
    return found[::-1]


def config(data_dir, rounds=3):
    return Config(data_dir=data_dir, ai=AISettings(writer=LOCAL, **dict.fromkeys(MEMBERS[1:], LOCAL)),
                  council=CouncilSettings(rounds=rounds))  # fmt: skip


@pytest.fixture
def models(monkeypatch):
    """Fake laptop models: the Writer answers GOOD; the Truth checker objects to the made-up claim, others approve."""
    calls = []

    def call(role, system, user, shape, **_):
        calls.append(user)
        if shape is flow.writer.SHAPE:
            text = GOOD if "<council>" not in user else json.dumps(json.loads(GOOD) | {"responses": []})
        elif system.startswith("You are the Truth checker") and "patent" in user:
            text = review({"where": "E2.1", "quote": QUOTE, "problem": "A patent the originals don't show."})
        else:
            text = review()
        return runner.llm.Reply(text, runner.llm.Usage(100, 0, 10))

    monkeypatch.setattr(runner.llm, "call", call)
    monkeypatch.setattr(runner, "_scoring", lambda config: False)
    return calls


def test_the_suggestion_is_the_newest_ready_jobs_one_per_company(conn, data_dir, jobs):
    resume = from_latex((data_dir / "resume.tex").read_text(encoding="utf-8")).fingerprint
    assert pilot.suggest(conn, config(data_dir), resume) == [jobs[0], jobs[1], jobs[2], jobs[4]]  # Beta and Acme once


def test_a_pilot_plants_five_made_up_claims_without_digits(conn, data_dir, jobs):
    number = pilot.create(conn, config(data_dir), jobs, seed=7)
    planted = json.loads(one(conn, "SELECT planted FROM pilots WHERE id = ?", number)[0])
    assert len(planted) == 5 and set(planted.values()) <= set(pilot.CLAIMS) and len(set(planted.values())) == 5
    assert all(not any(c.isdigit() for c in claim) for claim in pilot.CLAIMS)
    assert one(conn, "SELECT rounds, turns FROM pilots") == (3, 1)


def test_a_line_up_runs_on_every_job_apart_from_your_versions_and_never_as_a_pdf(conn, data_dir, jobs, models, pdfs):
    cfg = config(data_dir, rounds=1)  # the pilot keeps its own round limit of 3
    number = pilot.create(conn, cfg, jobs[:2], seed=1)
    planted = json.loads(one(conn, "SELECT planted FROM pilots")[0])
    with conn:
        conn.execute("UPDATE pilots SET planted = ?", (json.dumps({str(jobs[0]): "for which a patent was granted"}),))
    assert pilot.run(conn, cfg, number, "laptop", LAPTOP) == 2 and pilot.run(conn, cfg, number, "laptop", LAPTOP) == 0
    assert planted  # (the pilot chose its own; the test plants a known one)
    runner.run(conn, cfg, runner.State(), NOW)
    rows = conn.execute("SELECT kind, lineup, status, planted FROM preparations ORDER BY job_id DESC").fetchall()
    assert [row[:3] for row in rows] == [("pilot", "laptop", "done")] * 2 and pdfs == []  # never a PDF
    assert json.loads(rows[0][3]) == {"source": "profile", "tile_id": "exp-1", "bullet": 0,
                                      "claim": "for which a patent was granted"}  # fmt: skip
    assert any("Trained a robot arm with PPO, for which a patent was granted." in user for user in models)
    assert flow.runs.latest(conn, jobs[0]) is None  # not one of your versions
    found = pilot.summary(conn, number, "laptop")
    assert (found["jobs"], found["finished"], found["planted"], found["caught"], found["kept"]) == (2, 2, 1, 1, 0)
    assert found["no_agreement"] == 1 and found["agreed"] == 1 and found["tokens"] > 0  # the Writer took it out
    assert found["rounds"] == 2  # (3 rounds + 1) / 2: the pilot's own limit, not your setting of 1


def test_marks_and_using_a_result_without_a_made_up_claim(conn, data_dir, jobs, models, pdfs):
    cfg = config(data_dir)
    number = pilot.create(conn, cfg, jobs[:2], seed=1)
    with conn:
        conn.execute("UPDATE pilots SET planted = ?", (json.dumps({str(jobs[0]): "for which a patent was granted"}),))
    pilot.run(conn, cfg, number, "laptop", LAPTOP)
    runner.run(conn, cfg, runner.State(), NOW)
    result = pilot.results(conn, number, "laptop", jobs[0])
    objection = result["objections"][0]
    pilot.mark(conn, objection["step"], objection["id"], True)
    pilot.mark(conn, objection["step"], objection["id"], False)  # changed your mind
    assert pilot.summary(conn, number, "laptop")["not_useful"] == 1
    assert pilot.use(conn, result["id"]) is None  # it holds a made-up claim: never yours
    clean = pilot.results(conn, number, "laptop", jobs[1])
    copied = pilot.use(conn, clean["id"])
    assert one(conn, "SELECT kind, version, status FROM preparations WHERE id = ?", copied) == ("resume", 1, "done")


@pytest.fixture
def client(client, data_dir, tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(f"data_dir: {data_dir}\nai:\n" + "".join(f"  {m}:\n    model: gemma4:26b-a4b-it-q8_0\n"
                                                           for m in MEMBERS), encoding="utf-8")  # fmt: skip
    monkeypatch.setattr(settings, "CONFIG_PATH", path)
    monkeypatch.setattr(client.app.state.preparer, "wake", lambda: None)
    log_in(client)
    return client


def test_the_pilot_page_starts_a_pilot_runs_a_line_up_and_marks(client, conn, data_dir, jobs, pdfs):
    text = client.get("/pilot").text
    assert text.count('type="checkbox"') == 6 and text.count(" checked>") == 4  # suggested: one per company
    client.post("/pilot", data={"csrf_token": csrf(client), f"job_{jobs[0]}": "on", f"job_{jobs[1]}": "on"})
    text = client.get("/pilot").text
    assert "2 jobs, round limit 3" in text and ">Run Laptop</button>" in text and ">Run High</button>" in text
    client.post("/pilot/1/run/laptop", data={"csrf_token": csrf(client)})
    assert one(conn, "SELECT count(*) FROM preparations WHERE lineup = 'laptop'")[0] == 2
    assert "2 jobs" in client.get("/pilot").text and ">Run Laptop</button>" not in client.get("/pilot").text
    assert client.post("/pilot/1/run/cheapest", data={"csrf_token": csrf(client)}).status_code == 404
    assert "not run" in client.get(f"/pilot/1/jobs/{jobs[0]}").text
    assert client.post("/pilot", data={"csrf_token": csrf(client)}).status_code == 400  # no jobs ticked


def test_use_this_one_builds_its_pdf_apart_and_names_a_privacy_problem(client, conn, data_dir, jobs, models, pdfs,
                                                                      monkeypatch):  # fmt: skip
    cfg = config(data_dir)
    number = pilot.create(conn, cfg, jobs[:1], seed=1)
    with conn:
        conn.execute("UPDATE pilots SET planted = '{}'")
    pilot.run(conn, cfg, number, "laptop", LAPTOP)
    runner.run(conn, cfg, runner.State(), NOW)
    stopped = privacy.PrivacyError("Your name is missing from the Questionnaire.")
    monkeypatch.setattr(flow.pdf, "build_kept", lambda *args: (_ for _ in ()).throw(stopped))
    reply = client.post(f"/pilot/use/{pilot.results(conn, number, 'laptop', jobs[0])['id']}",
                        data={"csrf_token": csrf(client)}, follow_redirects=False)  # fmt: skip
    assert (reply.status_code, reply.headers["location"]) == (303, f"/jobs/{jobs[0]}#preparation")
    assert one(conn, "SELECT status, problem FROM preparations WHERE kind = 'resume'") == (
        "done", "PDF could not be built: Your name is missing from the Questionnaire.",
    )  # fmt: skip


@pytest.mark.parametrize(
    ("round_", "severity", "caught"),
    [(1, "must", 1), (1, "should", 0), (2, "must", 0)],
)
def test_a_made_up_claim_is_caught_only_by_a_truth_checker_must_in_round_1(conn, data_dir, jobs, round_, severity,
                                                                             caught):  # fmt: skip
    number = pilot.create(conn, config(data_dir), jobs[:1], seed=1)
    claim = "for which a patent was granted"
    version = flow.start(conn, config(data_dir), jobs[0], pilot=(number, "laptop", LAPTOP))
    bullets = [{"text": f"Built it, {claim}.", "from": [0]}]
    draft = {"entries": [{"source": "profile", "tile_id": "exp-1", "bullets": bullets, "skills": []}]}
    with conn:
        planted = json.dumps({"source": "profile", "tile_id": "exp-1", "bullet": 0, "claim": claim})
        conn.execute("UPDATE preparations SET status = 'done', round = 1, draft = ?, planted = ?, finished_on = "
                     "created_on WHERE id = ?", (json.dumps(draft), planted, version))  # fmt: skip
        answer = json.loads(review({"where": "E2", "severity": severity, "quote": QUOTE}))
        conn.execute("INSERT INTO steps (preparation_id, round, member, service, model, status, answer, day, sent_on) "
                     "VALUES (?, ?, 'truth', 'ollama', 'm', 'done', ?, 'd', 'now')",
                     (version, round_, json.dumps(answer | {"verdict": "object"})))  # fmt: skip
    found = pilot.summary(conn, number, "laptop")
    assert (found["caught"], found["kept"]) == (caught, 1)  # still in the final wording here


def test_a_pilot_runs_whatever_the_settings_say(conn, data_dir, jobs):
    empty = Config(data_dir=data_dir)  # no Writer and no council chosen
    number = pilot.create(conn, empty, jobs[:1], seed=1)
    assert pilot.run(conn, empty, number, "laptop", LAPTOP) == 1
    writer, council = one(conn, "SELECT writer, council FROM preparations")
    assert json.loads(writer)["model"] == "gemma4:26b-a4b-it-q8_0"
    assert json.loads(council)["members"]["truth"]["model"] == "qwen3.6:35b-a3b"
