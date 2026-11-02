"""The council's runner beside the dashboard: a pass after a press or each minute, one at a time, never two places at
once (fake passes here: nothing reaches an AI)."""

import threading
from contextlib import closing

import pytest

from app import collect
from app.config import ConfigError
from app.council import runner
from app.web import preparer as preparing


@pytest.fixture
def passes(data_dir, monkeypatch):
    """Each pass recorded (automatic preparation, then the runner) instead of really run."""
    found = []
    monkeypatch.setattr(preparing, "load_config", lambda: "the settings")
    monkeypatch.setattr(preparing.automatic, "start_due", lambda conn, config: found.append("automatic") or "waits")
    monkeypatch.setattr(preparing.runner, "run", lambda conn, config, state: found.append(("run", state)))
    return found


def wait_for(preparer):
    thread = preparer._thread
    if thread is not None:
        thread.join(5)


def test_a_press_runs_a_pass_with_automatic_preparation_first(data_dir, passes):
    found = preparing.Preparer(data_dir)
    found.wake()
    wait_for(found)
    assert passes == ["automatic", ("run", found.state)] and found.waiting == "waits" and found.notice is None
    assert found._thread is None  # nothing left to do: the next press starts a new pass


def test_presses_during_a_pass_make_one_more_pass(data_dir, passes, monkeypatch):
    found, held, release = preparing.Preparer(data_dir), threading.Event(), threading.Event()

    def slow(conn, config, state):
        passes.append("run")
        held.set()
        release.wait(5)

    monkeypatch.setattr(preparing.runner, "run", slow)
    found.wake()
    held.wait(5)
    found.wake()
    found.wake()  # two presses while it runs: one more pass is enough
    release.set()
    wait_for(found)
    assert passes.count("run") == 2


def test_a_runner_elsewhere_lets_this_one_wait_and_says_so(data_dir, passes):
    found = preparing.Preparer(data_dir)
    with closing(collect.lock(data_dir, runner.LOCK, "busy")):  # the terminal command, say
        found.wake()
        wait_for(found)
    assert passes == [] and found.notice == preparing.BUSY


def test_settings_that_cannot_be_read_are_named(data_dir, passes, monkeypatch):
    monkeypatch.setattr(preparing, "load_config", lambda: (_ for _ in ()).throw(ConfigError("Invalid settings.")))
    found = preparing.Preparer(data_dir)
    found.wake()
    wait_for(found)
    assert found.notice == "The settings can't be read: Invalid settings."


def test_saving_a_key_or_settings_tries_paused_services_again(data_dir, passes):
    found = preparing.Preparer(data_dir)
    found.state.paused["anthropic"] = "Anthropic refused the key."
    found.resume()
    wait_for(found)
    assert found.state.paused == {} and len(passes) == 2
