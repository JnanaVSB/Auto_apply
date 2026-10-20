import functools
import os
import shutil
import sqlite3
import stat
import sys
from contextlib import closing

import pytest
import sqlcipher3

from app import db
from app.config import Config
from app.keychain import KeychainError, get_secret, set_secret

PASSWORD = "fake-master-password-1"  # noqa: S105 (fake value, tests only)
NEW_PASSWORD = "fake-master-password-2"  # noqa: S105 (fake value, tests only)


@pytest.fixture(autouse=True)
def fast_kdf(monkeypatch):
    monkeypatch.setattr(db, "RECOVERY_KDF_ITER", 1_000)  # keeps tests quick; the app always uses the full count


@pytest.fixture
def data_dir(tmp_path):
    path = tmp_path / "data"
    db.setup(path, PASSWORD)
    return path


@pytest.fixture
def run(monkeypatch, capsys):
    """Run a terminal command against `folder`, typing `answers` at its password prompts."""

    def run(argv, folder, answers=()):
        replies = iter(answers)
        monkeypatch.setattr(db.getpass, "getpass", lambda prompt: next(replies))
        monkeypatch.setattr(db, "load_config", lambda: Config(data_dir=folder))
        return db.main(argv), capsys.readouterr().err

    return run


def test_setup_creates_files_and_daily_open_works(data_dir):
    assert {path.name for path in data_dir.iterdir()} == {db.DB_FILE, db.RECOVERY_FILE}
    with closing(db.connect(data_dir)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_key_and_password_never_on_disk_in_plain_text(data_dir):
    key_hex = get_secret(db.KEY_NAME)
    plain_secrets = [key_hex.encode(), bytes.fromhex(key_hex), PASSWORD.encode()]
    for path in data_dir.iterdir():
        content = path.read_bytes()
        assert not any(secret in content for secret in plain_secrets), path.name


def test_database_unreadable_without_the_key(data_dir):
    with closing(sqlite3.connect(data_dir / db.DB_FILE)) as plain, pytest.raises(sqlite3.DatabaseError):
        plain.execute("SELECT count(*) FROM sqlite_master")
    set_secret(db.KEY_NAME, "0" * 64)
    with pytest.raises(db.DatabaseError, match="could not be opened with the keychain key"):
        db.connect(data_dir)


def test_malformed_keychain_key_is_refused(data_dir):
    set_secret(db.KEY_NAME, "x'00'; DROP TABLE recovery")
    with pytest.raises(db.DatabaseError, match="malformed"):
        db.connect(data_dir)


def test_setup_never_overwrites(data_dir):
    before = (data_dir / db.DB_FILE).read_bytes()
    with pytest.raises(db.DatabaseError, match="never overwrites"):
        db.setup(data_dir, PASSWORD)
    assert (data_dir / db.DB_FILE).read_bytes() == before


def test_short_master_password_is_refused(tmp_path):
    with pytest.raises(db.DatabaseError, match="at least 12"):
        db.setup(tmp_path / "data", "too-short")
    assert not (tmp_path / "data").exists()


def test_failed_setup_leaves_no_files(tmp_path, monkeypatch):
    def broken_keychain(*args):
        raise KeychainError("fake keychain failure")

    monkeypatch.setattr(db, "set_secret", broken_keychain)
    with pytest.raises(KeychainError):
        db.setup(tmp_path / "data", PASSWORD)
    assert not any((tmp_path / "data").iterdir())


def test_lost_keychain_entry_is_recovered_with_master_password(data_dir, fake_keychain):
    key_hex = get_secret(db.KEY_NAME)
    fake_keychain.store.clear()
    with pytest.raises(db.DatabaseError, match="not found in the keychain"):
        db.connect(data_dir)
    with pytest.raises(db.DatabaseError, match="Wrong master password"):
        db.recover(data_dir, NEW_PASSWORD)
    assert get_secret(db.KEY_NAME) is None
    db.recover(data_dir, PASSWORD)
    assert get_secret(db.KEY_NAME) == key_hex
    db.connect(data_dir).close()


def test_change_master_password_relocks_only_the_recovery_file(data_dir):
    before = (data_dir / db.DB_FILE).read_bytes()
    db.change_master_password(data_dir, PASSWORD, NEW_PASSWORD)
    with pytest.raises(db.DatabaseError, match="Wrong master password"):
        db.recover(data_dir, PASSWORD)
    db.recover(data_dir, NEW_PASSWORD)
    assert (data_dir / db.DB_FILE).read_bytes() == before


@pytest.mark.skipif(sys.platform == "win32", reason="Windows does not use Unix file permissions")
def test_data_folder_and_files_are_private_to_the_user(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    data_dir.chmod(0o775)  # already exists and is open, as when created by hand for config.yaml
    db.setup(data_dir, PASSWORD)
    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
    for path in data_dir.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize(("answers", "code"), [([PASSWORD, PASSWORD], 0), ([PASSWORD, NEW_PASSWORD], 1)])
def test_setup_command(tmp_path, monkeypatch, capsys, answers, code):
    replies = iter(answers)
    monkeypatch.setattr(db.getpass, "getpass", lambda prompt: next(replies))
    monkeypatch.setattr(db, "load_config", lambda: Config(data_dir=tmp_path / "data"))
    assert db.main(["setup"]) == code
    assert (tmp_path / "data" / db.DB_FILE).exists() == (code == 0)
    assert PASSWORD not in capsys.readouterr().err


@pytest.mark.parametrize(("folder", "reason"), [("data", "never overwrites"), ("other", "won't replace")])
def test_setup_command_refuses_before_asking_for_a_password(data_dir, monkeypatch, capsys, folder, reason):
    def no_prompt(prompt):
        raise AssertionError("asked for a password")

    monkeypatch.setattr(db.getpass, "getpass", no_prompt)
    monkeypatch.setattr(db, "load_config", lambda: Config(data_dir=data_dir.parent / folder))
    assert db.main(["setup"]) == 1
    assert reason in capsys.readouterr().err


def test_change_password_command_checks_current_password_first(data_dir, monkeypatch, capsys):
    prompts = []
    monkeypatch.setattr(db.getpass, "getpass", lambda prompt: prompts.append(prompt) or NEW_PASSWORD)
    monkeypatch.setattr(db, "load_config", lambda: Config(data_dir=data_dir))
    assert db.main(["change-password"]) == 1
    assert prompts == ["Current master password: "]
    assert "Wrong master password" in capsys.readouterr().err


@pytest.mark.parametrize("password", ["it's-a-pass'word", "x'; ATTACH 'evil.db' AS e; --pad"])
def test_master_password_with_quote_marks_works(tmp_path, monkeypatch, fake_keychain, password):
    monkeypatch.chdir(tmp_path)  # a password that escaped into SQL would create evil.db here
    db.setup(tmp_path / "data", password)
    fake_keychain.store.clear()
    db.recover(tmp_path / "data", password)
    db.connect(tmp_path / "data").close()
    assert not list(tmp_path.rglob("evil.db"))


def test_recover_refuses_a_recovery_file_from_another_setup(data_dir, tmp_path):
    key_hex = get_secret(db.KEY_NAME)
    db.setup(tmp_path / "other", PASSWORD, replace_key=True)
    set_secret(db.KEY_NAME, key_hex)
    (tmp_path / "other" / db.RECOVERY_FILE).replace(data_dir / db.RECOVERY_FILE)
    with pytest.raises(db.DatabaseError, match="does not open app.db"):
        db.recover(data_dir, PASSWORD)
    assert get_secret(db.KEY_NAME) == key_hex


def test_connect_with_app_db_missing_creates_no_file(data_dir):
    (data_dir / db.DB_FILE).unlink()
    with pytest.raises(db.DatabaseError, match="app.db not found.*Check data_dir"):
        db.connect(data_dir)
    assert not (data_dir / db.DB_FILE).exists()


def test_recovery_file_uses_the_custom_kdf_rounds(data_dir):
    recovery, key = data_dir / db.RECOVERY_FILE, db._passphrase(PASSWORD)
    with pytest.raises(db._WrongKey):
        db._connect(recovery, key)  # SQLCipher's default rounds
    db._connect(recovery, key, db.RECOVERY_KDF_ITER).close()


def test_setup_never_replaces_an_existing_keychain_key(data_dir, tmp_path):
    key_hex = get_secret(db.KEY_NAME)
    with pytest.raises(db.DatabaseError, match="setup won't replace it"):
        db.setup(tmp_path / "other", PASSWORD)
    assert get_secret(db.KEY_NAME) == key_hex
    assert not (tmp_path / "other").exists()
    db.setup(tmp_path / "other", PASSWORD, replace_key=True)
    assert get_secret(db.KEY_NAME) != key_hex


def test_setup_command_with_replace_key(data_dir, tmp_path, monkeypatch):
    key_hex = get_secret(db.KEY_NAME)
    monkeypatch.setattr(db.getpass, "getpass", lambda prompt: PASSWORD)
    monkeypatch.setattr(db, "load_config", lambda: Config(data_dir=tmp_path / "other"))
    assert db.main(["setup", "--replace-key"]) == 0
    assert {path.name for path in (tmp_path / "other").iterdir()} == {db.DB_FILE, db.RECOVERY_FILE}
    assert get_secret(db.KEY_NAME) != key_hex


def test_replace_key_only_works_with_setup():
    with pytest.raises(SystemExit):
        db.main(["recover", "--replace-key"])


@pytest.mark.parametrize(
    "damage", ["DELETE FROM recovery", "DROP TABLE recovery", "UPDATE recovery SET db_key = 'not-a-key'"]
)
def test_damaged_recovery_file_gives_a_clear_error(data_dir, damage):
    key_hex = get_secret(db.KEY_NAME)
    conn, _ = db._open_recovery(data_dir, PASSWORD)
    with closing(conn):
        conn.execute(damage)
        conn.commit()
    with pytest.raises(db.DatabaseError, match="recovery.key is damaged"):
        db.recover(data_dir, PASSWORD)
    assert get_secret(db.KEY_NAME) == key_hex


@pytest.mark.parametrize("action", [db.recover, lambda path, pw: db.change_master_password(path, pw, NEW_PASSWORD)])
def test_missing_recovery_file_points_to_new_recovery_key(data_dir, action):
    (data_dir / db.RECOVERY_FILE).unlink()
    with pytest.raises(db.DatabaseError, match="new-recovery-key") as error:
        action(data_dir, PASSWORD)
    assert "setup" not in str(error.value)


def test_damaged_app_db_is_not_blamed_on_the_recovery_file(data_dir):
    with (data_dir / db.DB_FILE).open("r+b") as file:
        file.seek(100)
        file.write(bytes(100))
    with pytest.raises(db.DatabaseError, match="the key is wrong or the file is damaged"):
        db.connect(data_dir)
    with pytest.raises(db.DatabaseError, match="or app.db is damaged"):
        db.recover(data_dir, PASSWORD)


def test_new_recovery_key_replaces_a_lost_recovery_file(data_dir):
    key_hex = get_secret(db.KEY_NAME)
    (data_dir / db.RECOVERY_FILE).unlink()
    db.new_recovery_key(data_dir, NEW_PASSWORD)
    db.recover(data_dir, NEW_PASSWORD)
    assert get_secret(db.KEY_NAME) == key_hex
    assert {path.name for path in data_dir.iterdir()} == {db.DB_FILE, db.RECOVERY_FILE}


def test_new_recovery_key_replaces_the_old_master_password(data_dir):
    db.new_recovery_key(data_dir, NEW_PASSWORD)
    with pytest.raises(db.DatabaseError, match="Wrong master password"):
        db.recover(data_dir, PASSWORD)
    db.recover(data_dir, NEW_PASSWORD)


def test_new_recovery_key_refuses_a_key_that_does_not_open_app_db(data_dir):
    before = (data_dir / db.RECOVERY_FILE).read_bytes()
    set_secret(db.KEY_NAME, "0" * 64)
    with pytest.raises(db.DatabaseError, match="could not be opened"):
        db.new_recovery_key(data_dir, NEW_PASSWORD)
    assert (data_dir / db.RECOVERY_FILE).read_bytes() == before
    assert {path.name for path in data_dir.iterdir()} == {db.DB_FILE, db.RECOVERY_FILE}


def test_new_recovery_key_command_checks_the_keychain_before_asking(data_dir, fake_keychain, monkeypatch, capsys):
    def no_prompt(prompt):
        raise AssertionError("asked for a password")

    fake_keychain.store.clear()
    monkeypatch.setattr(db.getpass, "getpass", no_prompt)
    monkeypatch.setattr(db, "load_config", lambda: Config(data_dir=data_dir))
    assert db.main(["new-recovery-key"]) == 1
    assert "not found in the keychain" in capsys.readouterr().err


def test_interrupted_new_recovery_key_keeps_the_old_file(data_dir, monkeypatch):
    before = (data_dir / db.RECOVERY_FILE).read_bytes()

    def crash(path, *args):
        path.write_bytes(b"half-written")
        raise KeyboardInterrupt

    monkeypatch.setattr(db, "_write_recovery", crash)
    with pytest.raises(KeyboardInterrupt):
        db.new_recovery_key(data_dir, NEW_PASSWORD)
    assert (data_dir / db.RECOVERY_FILE).read_bytes() == before
    assert {path.name for path in data_dir.iterdir()} == {db.DB_FILE, db.RECOVERY_FILE}


skip_without_unix_permissions = pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0, reason="needs Unix file permissions and a non-root user"
)


@skip_without_unix_permissions
@pytest.mark.parametrize(("file", "action"), [(db.RECOVERY_FILE, "recover"), (db.DB_FILE, "connect")])
def test_unreadable_file_is_not_called_a_wrong_key(data_dir, file, action):
    (data_dir / file).chmod(0)
    with pytest.raises(db.DatabaseError, match=f"{file} could not be read") as error:
        db.recover(data_dir, PASSWORD) if action == "recover" else db.connect(data_dir)
    assert "Wrong master password" not in str(error.value)
    assert "key is wrong" not in str(error.value)


def test_locked_app_db_says_locked(data_dir, monkeypatch):
    with closing(db.connect(data_dir)) as other_program:
        other_program.execute("BEGIN EXCLUSIVE")
        monkeypatch.setattr(db.sqlcipher3, "connect", functools.partial(sqlcipher3.connect, timeout=0.1))
        with pytest.raises(db.DatabaseError, match="app.db could not be read \\(database is locked\\)"):
            db.connect(data_dir)


def test_interrupted_setup_can_be_retried_and_keeps_the_leftover(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db._write_recovery(data_dir / db.RECOVERY_FILE, PASSWORD, "a" * 64)  # setup stopped before app.db
    leftover = (data_dir / db.RECOVERY_FILE).read_bytes()
    db.setup(data_dir, PASSWORD)
    assert (data_dir / f"old-{db.RECOVERY_FILE}").read_bytes() == leftover
    db.connect(data_dir).close()


def test_failed_setup_leaves_the_keychain_unchanged(data_dir, tmp_path, monkeypatch):
    key_hex = get_secret(db.KEY_NAME)

    def disk_full(*args):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(db, "_raw_key", disk_full)  # creating app.db fails, after recovery.key was written
    with pytest.raises(OSError):
        db.setup(tmp_path / "other", PASSWORD, replace_key=True)
    assert get_secret(db.KEY_NAME) == key_hex  # even --replace-key lost nothing
    assert not any((tmp_path / "other").iterdir())


def test_empty_app_db_is_refused(data_dir):
    key_hex = get_secret(db.KEY_NAME)
    (data_dir / db.DB_FILE).write_bytes(b"")
    with pytest.raises(db.DatabaseError, match="app.db is empty or damaged"):
        db.connect(data_dir)
    with pytest.raises(db.DatabaseError, match="app.db is empty or damaged"):
        db.recover(data_dir, PASSWORD)
    assert get_secret(db.KEY_NAME) == key_hex


def test_empty_recovery_file_is_refused(data_dir, run):
    (data_dir / db.RECOVERY_FILE).write_bytes(b"")
    with pytest.raises(db.DatabaseError, match="recovery.key is damaged"):
        db.change_master_password(data_dir, "any-wrong-password", NEW_PASSWORD)
    code, err = run(["change-password"], data_dir, ["any-wrong-password"])  # refused before asking for a new one
    assert code == 1
    assert "recovery.key is damaged" in err


def test_commands_before_setup_point_to_setup(tmp_path):
    for action in (lambda: db.recover(tmp_path, PASSWORD), lambda: db.connect(tmp_path)):
        with pytest.raises(db.DatabaseError, match="app.db not found.*run: python -m app.db setup"):
            action()


@pytest.mark.parametrize("password", ["aaaaaaaaaaaa", "123456789012", "abcdefghijklm", "QWERTYUIOPQW", "9876543210987"])
def test_weak_master_password_is_refused(tmp_path, password):
    with pytest.raises(db.DatabaseError, match="too easy to guess"):
        db.setup(tmp_path / "data", password)


def test_null_character_in_a_password_is_refused(data_dir, tmp_path):
    for action in (
        lambda: db.setup(tmp_path / "x", "fake-pass\0word-1", replace_key=True),
        lambda: db.recover(data_dir, "a\0b"),
    ):
        with pytest.raises(db.DatabaseError, match="null"):
            action()


def test_non_english_master_password_works(tmp_path, fake_keychain):
    db.setup(tmp_path / "data", "pässwörter-日本語-test")
    fake_keychain.store.clear()
    db.recover(tmp_path / "data", "pässwörter-日本語-test")


def test_recover_can_run_twice(data_dir):
    db.recover(data_dir, PASSWORD)
    db.recover(data_dir, PASSWORD)
    db.connect(data_dir).close()


def test_restored_data_folder_recovers_on_a_new_laptop(data_dir, tmp_path, fake_keychain):
    new_laptop = tmp_path / "new-laptop-data"
    shutil.copytree(data_dir, new_laptop)
    fake_keychain.store.clear()
    db.recover(new_laptop, PASSWORD)
    db.connect(new_laptop).close()


def test_recover_command(data_dir, fake_keychain, run):
    fake_keychain.store.clear()
    assert run(["recover"], data_dir, [PASSWORD]) == (0, "")
    db.connect(data_dir).close()


def test_change_password_command(data_dir, run):
    assert run(["change-password"], data_dir, [PASSWORD, NEW_PASSWORD, NEW_PASSWORD]) == (0, "")
    db.recover(data_dir, NEW_PASSWORD)


def test_new_recovery_key_command(data_dir, run):
    assert run(["new-recovery-key"], data_dir, [NEW_PASSWORD, NEW_PASSWORD]) == (0, "")
    db.recover(data_dir, NEW_PASSWORD)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows does not use Unix file permissions")
def test_new_recovery_key_file_is_private_to_the_user(data_dir):
    db.new_recovery_key(data_dir, NEW_PASSWORD)
    assert stat.S_IMODE((data_dir / db.RECOVERY_FILE).stat().st_mode) == 0o600


def test_sqlcipher_log_stays_silent_on_a_wrong_password(data_dir, capfd):
    with pytest.raises(db.DatabaseError):
        db.recover(data_dir, NEW_PASSWORD)
    assert "ERROR" not in capfd.readouterr().err


@pytest.mark.parametrize("interruption", [EOFError, KeyboardInterrupt])
def test_cancelling_at_a_prompt_prints_cancelled(data_dir, monkeypatch, capsys, interruption):
    def cancel(prompt):
        raise interruption

    monkeypatch.setattr(db.getpass, "getpass", cancel)
    monkeypatch.setattr(db, "load_config", lambda: Config(data_dir=data_dir))
    assert db.main(["recover"]) == 1
    assert "Cancelled." in capsys.readouterr().err


def test_file_problem_gives_a_short_message(tmp_path, run):
    (tmp_path / "data").write_text("not a folder")
    code, err = run(["setup"], tmp_path / "data", [PASSWORD, PASSWORD])
    assert code == 1
    assert err.strip() == "Error: File exists: data"


def test_unexpected_database_error_gives_a_short_message(data_dir, monkeypatch, run):
    def disk_error(*args):
        raise sqlcipher3.OperationalError("disk I/O error")

    monkeypatch.setattr(db, "recover", disk_error)
    code, err = run(["recover"], data_dir, [PASSWORD])
    assert code == 1
    assert err.strip() == "Error: the database could not be read or written (disk I/O error)."
