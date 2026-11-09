"""`python -m app.setup`, with the fake keychain and stand-ins for the database, scoring and dashboard commands."""

import getpass
import sys
from pathlib import Path

import pytest
from keyring.errors import KeyringLocked

from app import config, db, keychain, score, setup
from app.scoring import files
from app.web import __main__ as web_command

SETTINGS = b"data_dir: {data}\n# made-up settings of the test's own\n"


class Answers:
    """Typed answers to setup's questions, in order; a question with no answer left fails the test."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.questions = []

    def __call__(self, question):
        self.questions.append(question)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


@pytest.fixture
def ran(tmp_path, monkeypatch):
    """Settings of the test's own in a throwaway folder; the commands setup hands over to only record that they ran."""
    path = tmp_path / "data" / "config.yaml"
    path.parent.mkdir()
    path.write_bytes(SETTINGS.replace(b"{data}", str(tmp_path / "data").encode()))
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": pytest.fail("asked for a password"))
    monkeypatch.setattr(files, "_correct", lambda path, file: True)
    calls = []
    for module, code in ((db, 0), (score, 1), (web_command, 0)):  # 1: the scoring model isn't ready, as at first
        monkeypatch.setattr(module, "main", lambda argv, module=module, code=code: calls.append(module) or code)
    return calls


@pytest.fixture
def later(tmp_path, monkeypatch):
    """A project folder and a Python path with spaces in them; returns the start of the quoted command setup prints."""
    monkeypatch.setattr(config, "PROJECT_DIR", tmp_path / "my projects" / "auto_apply")
    monkeypatch.setattr(sys, "executable", "/home/fake user/miniforge3/envs/auto_apply/bin/python")
    return f"cd '{tmp_path}/my projects/auto_apply' && '/home/fake user/miniforge3/envs/auto_apply/bin/python'"


def answering(monkeypatch, *answers):
    answers = Answers(*answers)
    monkeypatch.setattr("builtins.input", answers)
    return answers


@pytest.mark.parametrize(
    ("platform", "fix"),
    [
        ("linux", "This computer has no keychain the app can use."),
        ("darwin", 'click "Always Allow" when macOS asks'),
        ("win32", "(found: conftest)."),
    ],
)
def test_no_keychain_stops_before_any_password_with_the_fix(ran, fake_keychain, monkeypatch, capsys, platform, fix):
    monkeypatch.setattr(keychain, "SAFE_BACKENDS", ("keyring.backends.macOS",))  # the fake one is no longer safe
    monkeypatch.setattr(sys, "platform", platform)
    assert setup.main([]) == 1
    err = capsys.readouterr().err
    assert err.startswith("Error: No secure OS keychain available (found: conftest).")
    assert fix in err
    assert "locked or refused" not in err
    assert ran == []
    assert fake_keychain.store == {}


@pytest.mark.parametrize(
    ("platform", "fix"),
    [("linux", "The keychain is locked or refused"), ("darwin", 'click "Always Allow"'), ("win32", "")],
)
def test_a_refused_save_stops_before_any_password_with_the_fix(ran, fake_keychain, monkeypatch, capsys, platform, fix):
    def refused(service, username, password):
        raise KeyringLocked("Failed to unlock the collection!")

    monkeypatch.setattr(fake_keychain, "set_password", refused)
    monkeypatch.setattr(sys, "platform", platform)
    assert setup.main([]) == 1
    err = capsys.readouterr().err
    assert err.startswith("Error: Could not save 'setup-check' in the OS keychain (KeyringLocked).")
    assert fix in err
    assert "no keychain the app can use" not in err
    assert ran == []
    assert fake_keychain.store == {}


def test_a_different_value_read_back_stops_and_the_entry_is_removed(ran, fake_keychain, monkeypatch, capsys):
    monkeypatch.setattr(fake_keychain, "get_password", lambda service, username: "something else")
    monkeypatch.setattr(sys, "platform", "linux")
    assert setup.main([]) == 1
    err = capsys.readouterr().err
    assert "Error: The OS keychain gave back a different value than the one saved.\nThe keychain is locked" in err
    assert ran == []
    assert fake_keychain.store == {}


def test_a_second_run_asks_only_about_the_dashboard_and_changes_nothing(
    ran, tmp_path, fake_keychain, monkeypatch, capsys
):
    (tmp_path / "data" / db.DB_FILE).write_bytes(b"made-up database")
    before = {path: path.read_bytes() for path in (tmp_path / "data").iterdir()}
    answers = answering(monkeypatch, "n")
    assert setup.main([]) == 0
    assert answers.questions == ["Start the dashboard now? [y/N] "]
    assert {path: path.read_bytes() for path in (tmp_path / "data").iterdir()} == before
    assert ran == [score]  # files all there: only the model check, which may say "not ready" without stopping setup
    assert fake_keychain.store == {}  # the test entry removed again
    out = capsys.readouterr().out
    assert out.startswith("Keychain: works.\nSettings: data/config.yaml is already there; left as it is.\n")
    assert "Database: already set up." in out


def test_a_missing_database_is_set_up_and_its_failure_stops_setup(ran, monkeypatch):
    answering(monkeypatch, "n")
    assert setup.main([]) == 0
    assert ran == [db, score]
    monkeypatch.setattr(db, "main", lambda argv: ran.append("failed") or 1)
    assert setup.main([]) == 1
    assert ran[2:] == ["failed"]


def test_missing_scoring_files_are_offered_with_their_size(ran, later, tmp_path, monkeypatch, capsys):
    (tmp_path / "data" / db.DB_FILE).write_bytes(b"made-up database")
    monkeypatch.setattr(files, "_correct", lambda path, file: file is files.SKILLS)
    answers = answering(monkeypatch, "  N ", "no")
    assert setup.main([]) == 0
    assert answers.questions[0] == "Download the scoring files now (about 134 MB)? [y/N] "
    assert ran == []
    assert (
        f"Scoring files not downloaded; get them later with:\n  {later} -m app.score setup\n" in capsys.readouterr().out
    )
    answering(monkeypatch, " Yes ", "n")
    assert setup.main([]) == 0
    assert ran == [score]
    assert "not downloaded" not in capsys.readouterr().out


def test_scoring_not_ready_says_how_to_check_again_and_setup_goes_on(ran, later, tmp_path, monkeypatch, capsys):
    (tmp_path / "data" / db.DB_FILE).write_bytes(b"made-up database")
    answering(monkeypatch, "n")
    assert setup.main([]) == 0  # the stand-in scoring check says "not ready"
    out = capsys.readouterr().out
    again = (
        f"Scoring isn't ready yet (see the line above); once that's fixed, check again with:\n  {later} -m app.score"
    )
    assert f"{again} setup\n" in out
    assert "Setup is done." in out
    monkeypatch.setattr(score, "main", lambda argv: 0)
    answering(monkeypatch, "n")
    assert setup.main([]) == 0
    assert "isn't ready" not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("ai", "note"),
    [
        ("", "AI scoring runs through Ollama on this computer: install it and its model"),
        (
            "ai:\n  evaluator: {service: openai-compatible, model: made-up-model, "
            "address: 'https://scoring.example.net/v1'}\n",
            "The scoring model is scoring.example.net; change it on Settings → Scoring model",
        ),
    ],
)
def test_the_scoring_note_names_ollama_or_the_address(ran, monkeypatch, capsys, ai, note):
    with config.CONFIG_PATH.open("a") as file:
        file.write(ai)
    answering(monkeypatch, "n")
    assert setup.main([]) == 0
    assert note in capsys.readouterr().out


def test_the_start_command_has_the_real_paths_and_yes_starts_the_dashboard(ran, later, monkeypatch, capsys):
    answering(monkeypatch, "n")
    assert setup.main([]) == 0
    assert f"To open the dashboard later, run:\n  {later} -m app.web\n" in capsys.readouterr().out
    assert web_command not in ran
    monkeypatch.setattr(web_command, "main", lambda argv: 7)
    answering(monkeypatch, "y")
    assert setup.main([]) == 7  # the dashboard's own result


@pytest.mark.parametrize("stop", [KeyboardInterrupt(), EOFError()])
@pytest.mark.parametrize("missing_files", [True, False])
def test_ctrl_c_or_ctrl_d_at_a_question_stops_cleanly(ran, monkeypatch, capsys, stop, missing_files):
    monkeypatch.setattr(files, "_correct", lambda path, file: not missing_files)
    answering(monkeypatch, stop)  # the first question: the scoring files when some are missing, else the dashboard
    assert setup.main([]) == 1
    assert capsys.readouterr().err == "\nStopped. Nothing done so far is undone; run ./setup.sh again to continue.\n"
    assert web_command not in ran


def test_settings_are_copied_from_the_example_only_if_absent(tmp_path, monkeypatch):
    path = tmp_path / "data" / "config.yaml"
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    setup._copy_config()
    assert path.read_bytes() == (config.PROJECT_DIR / "config.example.yaml").read_bytes()
    assert path.parent.stat().st_mode & 0o777 == 0o700
    path.write_bytes(b"made-up settings")
    setup._copy_config()
    assert path.read_bytes() == b"made-up settings"


class CutOff:
    """A settings file whose writing is stopped by Ctrl+C after its first few letters."""

    def __init__(self, file):
        self.file = file

    def __enter__(self):
        return self

    def __exit__(self, *error):
        self.file.close()

    def write(self, text):
        self.file.write(text[:10])
        raise KeyboardInterrupt


def test_ctrl_c_while_writing_the_settings_leaves_no_half_file(tmp_path, monkeypatch):
    path = tmp_path / "data" / "config.yaml"
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    real_open = Path.open

    def cut_off(self, mode="r", *args, **kwargs):
        file = real_open(self, mode, *args, **kwargs)
        return CutOff(file) if mode == "x" else file

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", cut_off)
        with pytest.raises(KeyboardInterrupt):
            setup._copy_config()
    assert not path.exists()
    setup._copy_config()
    assert path.read_bytes() == (config.PROJECT_DIR / "config.example.yaml").read_bytes()


def test_a_file_where_the_data_folder_should_be_is_named(tmp_path, monkeypatch, capsys):
    (tmp_path / "data").write_bytes(b"not a folder")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "data" / "config.yaml")
    assert setup.main([]) == 1
    assert capsys.readouterr().err == (
        "Error: The project's data folder is a file: rename or move the file named data, then run ./setup.sh again.\n"
    )
