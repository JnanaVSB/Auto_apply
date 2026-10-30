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

    def __call__(self, conn, settings, job_id) -> None:
        self.started.append(job_id)
        assert self.go.wait(WAIT)
        if job_id == 3:
            self.done.set()


@pytest.fixture
def fake(monkeypatch, tmp_path, data_dir):
    path = tmp_path / "config.yaml"
    path.write_text(f"data_dir: {data_dir}\n", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    writer = FakeWriter()
    monkeypatch.setattr(preparing.runs, "prepare", writer)
    return writer


def finished(preparer):
    for _ in range(WAIT * 100):
        if preparer._thread is None:
            return True
        threading.Event().wait(0.01)
    return False


def test_one_job_at_a_time_the_others_wait_their_turn(data_dir, fake):
    preparer = Preparer(data_dir)
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
    preparer.start(4)  # after the line has emptied, a press starts the Writer again
    fake.go.set()
    assert finished(preparer) and fake.started == [1, 2, 3, 4]


def test_a_writer_running_elsewhere_lets_the_presses_go_and_says_why(data_dir, fake):
    preparer = Preparer(data_dir)
    with closing(collect.lock(data_dir, preparing.PREPARE_LOCK, "busy")):
        preparer.recover()  # nothing to do while another Writer runs
        preparer.start(1)
        assert finished(preparer)
    assert fake.started == [] and preparer.notice == preparing.BUSY and preparer.place(1) is None


def test_the_dashboard_marks_calls_an_earlier_one_left_waiting(data_dir, monkeypatch):
    found = []
    monkeypatch.setattr(preparing.runs, "recover", lambda conn: found.append(conn))
    Preparer(data_dir).recover()
    assert len(found) == 1
