"""The Writer beside the dashboard: one job at a time, the others waiting their turn, and its own lock."""

import threading
from contextlib import closing

import pytest

from app import collect, config
from app.web import preparer as preparing
from app.web.preparer import Preparer

WAIT = 5  # seconds a test waits for the Writer's thread at most


class FakeWriter:
    """Stands in for runs.prepare: each job waits until the test lets it go, so the test sees the line."""

    def __init__(self) -> None:
        self.started, self.go, self.done = [], threading.Event(), threading.Event()

    def __call__(self, conn, settings, job_id, feedback="", kind="resume") -> None:
        self.started.append((job_id, feedback or kind) if feedback or kind != "resume" else job_id)
        assert self.go.wait(WAIT)
        if job_id == 3:
            self.done.set()


@pytest.fixture
def fake(monkeypatch, tmp_path, data_dir):
    path = tmp_path / "config.yaml"
    path.write_text(f"data_dir: {data_dir}\nai:\n  writer:\n    model: gemma4:26b-a4b-it-q8_0\n", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    writer = FakeWriter()
    monkeypatch.setattr(preparing.runs, "prepare", writer)
    return writer


def startable(conn, job_id, waived):
    return True


def finished(preparer):
    for _ in range(WAIT * 100):
        if preparer._thread is None:
            return True
        threading.Event().wait(0.01)
    return False


def test_one_job_at_a_time_the_others_wait_their_turn(data_dir, fake):
    preparer = Preparer(data_dir, startable)
    for job_id in (1, 2, 1, 3):  # a second press on a job changes nothing
        preparer.start(job_id)
    for _ in range(WAIT * 100):
        if fake.started:
            break
        threading.Event().wait(0.01)
    assert (preparer.place(1), preparer.place(2), preparer.place(3), preparer.place(4)) == (0, 1, 2, None)
    fake.go.set()
    assert fake.done.wait(WAIT) and finished(preparer)
    assert fake.started == [1, 2, 3] and preparer.place(1) is None
    fake.go.clear()
    preparer.start(4, "Shorter.")  # after the line has emptied, a press starts the Writer again, your words kept
    preparer.start(5, "", "cover_letter")
    fake.go.set()
    assert finished(preparer) and fake.started == [1, 2, 3, (4, "Shorter."), (5, "cover_letter")]


def test_a_writer_running_elsewhere_lets_the_presses_go_and_says_why(data_dir, fake):
    preparer = Preparer(data_dir, startable)
    with closing(collect.lock(data_dir, preparing.PREPARE_LOCK, "busy")):
        preparer.recover()  # nothing to do while another Writer runs
        preparer.start(1)
        assert finished(preparer)
    assert fake.started == [] and preparer.notice == preparing.BUSY and preparer.place(1) is None


def test_the_dashboard_marks_calls_an_earlier_one_left_waiting(data_dir, monkeypatch):
    found = []
    monkeypatch.setattr(preparing.runs, "recover", lambda conn: found.append(conn))
    Preparer(data_dir, startable).recover()
    assert len(found) == 1


def test_a_job_skipped_closed_or_no_longer_ready_while_it_waits_is_let_go(data_dir, fake):
    asked = []
    preparer = Preparer(data_dir, lambda conn, job_id, waived: asked.append((job_id, waived)) or job_id != 2)
    preparer.start(1)
    preparer.start(2, waived=frozenset({"not-ready"}))  # the pop-up asked about this; something new came since
    preparer.start(3)
    fake.go.set()
    assert fake.done.wait(WAIT) and finished(preparer)
    assert fake.started == [1, 3] and asked == [(1, frozenset()), (2, frozenset({"not-ready"})), (3, frozenset())]
    assert preparer.notice == preparing.LET_GO


def test_the_writer_removed_in_settings_while_jobs_wait_lets_them_go_and_says_why(data_dir, fake):
    config.CONFIG_PATH.write_text(f"data_dir: {data_dir}\n", encoding="utf-8")
    preparer = Preparer(data_dir, startable)
    preparer.start(1)
    preparer.start(2)
    assert finished(preparer)
    assert fake.started == [] and preparer.notice == preparing.NO_WRITER and preparer.place(2) is None


def test_a_key_or_model_problem_lets_the_waiting_jobs_go_with_the_same_message(data_dir, fake, monkeypatch):
    refused = "Anthropic refused the key (status 401): check the key saved in the keychain."

    def halts(conn, settings, job_id, feedback="", kind="resume"):
        fake.started.append(job_id)
        assert fake.go.wait(WAIT)
        raise preparing.runs.Halted(refused)

    monkeypatch.setattr(preparing.runs, "prepare", halts)
    preparer = Preparer(data_dir, startable)
    for job_id in (1, 2, 3):
        preparer.start(job_id)
    fake.go.set()
    assert finished(preparer)
    assert fake.started == [1] and preparer.notice == preparing.HALTED.format(refused)
