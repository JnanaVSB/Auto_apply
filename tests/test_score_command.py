"""`python -m app.score`, with the downloads, the meaning model and the AI replaced by stand-ins."""

from contextlib import closing

import httpx
import pytest
from helpers import add_job
from test_stage_a import FakeMeaning

from app import collect, llm, score
from app.config import AIRole, AISettings, Config, RoleSettings, ScoringSettings
from app.llm import ollama
from app.resume import RESUME_FILE
from app.scoring import files, queue

OWN = "https://scoring.example.net/v1"
FAKE_RESUME = (
    rb"\begin{document}" + b"Made-up resume about robots and reinforcement learning. " * 10 + rb"\end{document}"
)


@pytest.fixture
def ready(data_dir, monkeypatch, tmp_path):
    """A data folder with a made-up resume; files, meaning model, skills list and AI are stand-ins."""
    (data_dir / RESUME_FILE).write_bytes(FAKE_RESUME)
    monkeypatch.setattr(score, "load_config", lambda: Config(data_dir=data_dir, roles=RoleSettings(keywords=["RL"])))
    monkeypatch.setattr(files, "ensure_files", lambda fetcher, out=print, folder=None: tmp_path)
    monkeypatch.setattr(score.Meaning, "load", lambda folder: FakeMeaning())
    monkeypatch.setattr(score.stage_a, "skill_names", lambda folder, extra: ["PyTorch"])
    monkeypatch.setattr(ollama, "answer", lambda *args: llm.Reply('{"score": 80, "reasons": ["Robots."]}', llm.Usage()))
    monkeypatch.setattr(queue, "today", lambda: "2026-10-28")
    with closing(score.db.connect(data_dir)) as conn, conn:
        add_job(conn, "RL Robot Engineer", description="Robots.", date_posted="2026-10-27")  # passes the quick check
    return data_dir


def test_scoring_runs_and_says_what_it_did(ready, capsys):
    assert score.main([]) == 0
    out = capsys.readouterr()
    assert out.err == ""
    assert "AI scoring with gemma4:26b-a4b-it-q8_0: 1 jobs waiting..." in out.out
    assert "Scored 1 jobs: 1 ready (70 or more), 0 low score" in out.out


def test_a_second_scoring_run_is_refused(ready, capsys):
    with closing(collect.lock(ready, score.SCORE_LOCK)):
        assert score.main([]) == 1
    assert "Scoring is already running" in capsys.readouterr().err


def test_a_missing_resume_is_one_clear_line(ready, capsys):
    (ready / RESUME_FILE).unlink()
    assert score.main([]) == 1
    assert capsys.readouterr().err.startswith("Error: Resume not found: save your LaTeX resume as resume.tex")


def test_ollama_not_running_stops_with_the_fix(ready, monkeypatch, capsys):
    def not_running(*args):
        raise llm.StopScoring("Ollama isn't running: start it with `ollama serve`.")

    monkeypatch.setattr(ollama, "answer", not_running)
    assert score.main([]) == 1
    assert "Error: Ollama isn't running: start it with `ollama serve`." in capsys.readouterr().err


def test_ctrl_c_says_what_is_kept(ready, monkeypatch, capsys):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(score.queue, "run", interrupted)
    assert score.main([]) == 1
    assert "Stopped. Everything scored so far is saved; run it again to continue." in capsys.readouterr().err


def test_setup_checks_the_files_and_ollama(ready, monkeypatch, capsys):
    monkeypatch.setattr(
        ollama, "status", lambda role, transport=None: (False, "Ollama isn't running: start it with `ollama serve`.")
    )
    assert score.main(["setup"]) == 1
    assert capsys.readouterr().out == (
        "Scoring files: all present and correct.\nOllama isn't running: start it with `ollama serve`.\n"
    )


def cloud(data_dir, ticked=OWN):
    """Scoring through a made-up address of your own, ticked or not."""
    role = AIRole(service="openai-compatible", model="fake-model", address=OWN)
    return Config(data_dir=data_dir, ai=AISettings(evaluator=role), scoring=ScoringSettings(cloud_ok=ticked))


def test_setup_checks_a_ticked_cloud_scorer_for_free(ready, capsys):
    asked = []

    def models(request):
        asked.append(request)
        return httpx.Response(200, json={"data": [{"id": "fake-model"}]})

    assert score.setup(cloud(ready), httpx.MockTransport(models)) is True
    assert capsys.readouterr().out == (
        "Scoring files: all present and correct.\nscoring.example.net accepted the request and knows the model "
        "fake-model.\n"
    )
    assert [(request.method, str(request.url), request.content) for request in asked] == [
        ("GET", f"{OWN}/models", b"")
    ]  # only its list of models: nothing of yours


def test_setup_refuses_a_cloud_scorer_whose_box_is_not_ticked(ready, monkeypatch, capsys):
    monkeypatch.setattr(score, "load_config", lambda: cloud(ready, ticked=""))
    monkeypatch.setattr(llm, "check", lambda *args: pytest.fail("the service was asked"))
    assert score.main(["setup"]) == 1
    assert "The box for sending your resume to scoring.example.net isn't ticked" in capsys.readouterr().err


def test_setup_is_not_ready_when_the_address_has_no_list_of_models(ready, capsys):
    assert score.setup(cloud(ready), httpx.MockTransport(lambda request: httpx.Response(404))) is False
    assert (
        "scoring.example.net answers, but doesn't list its models: your first use will show whether it works."
        in capsys.readouterr().out
    )


def test_opening_waits_for_a_collection_but_not_for_a_refresh(data_dir, monkeypatch):
    lines, sleeps = [], []
    held = collect.lock(data_dir)  # a collection is running

    def collection_ends(seconds):
        sleeps.append(seconds)
        held.close()

    monkeypatch.setattr(score.time, "sleep", collection_ends)
    with closing(score.open_database(data_dir, lines.append)):
        pass
    assert lines == [score.WAITING] and sleeps == [queue.PAUSE_CHECK_SECONDS]
    with closing(collect.lock(data_dir, collect.REFRESH_LOCK)), closing(collect.lock(data_dir)):  # a refresh
        with closing(score.open_database(data_dir, lines.append)):
            pass
    assert lines == [score.WAITING]  # no wait this time
