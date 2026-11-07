import functools
import os
import re
import shutil
import sqlite3
import stat
import sys
from contextlib import closing

import pytest
import sqlcipher3
from helpers import MASTER_PASSWORD as PASSWORD
from helpers import add_job

from app import db
from app.config import Config
from app.keychain import KeychainError, get_secret, set_secret

NEW_PASSWORD = "fake-master-password-2"  # noqa: S105 (fake value, tests only)
EXPORT_TABLES = ("export_runs", "exported")


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


@pytest.mark.usefixtures("data_dir")
def test_setup_never_replaces_an_existing_keychain_key(tmp_path):
    key_hex = get_secret(db.KEY_NAME)
    with pytest.raises(db.DatabaseError, match="setup won't replace it"):
        db.setup(tmp_path / "other", PASSWORD)
    assert get_secret(db.KEY_NAME) == key_hex
    assert not (tmp_path / "other").exists()
    db.setup(tmp_path / "other", PASSWORD, replace_key=True)
    assert get_secret(db.KEY_NAME) != key_hex


@pytest.mark.usefixtures("data_dir")
def test_setup_command_with_replace_key(tmp_path, monkeypatch):
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
        other_program.execute("PRAGMA locking_mode = EXCLUSIVE")  # in WAL mode an ordinary write never blocks readers
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


@pytest.mark.usefixtures("data_dir")
def test_failed_setup_leaves_the_keychain_unchanged(tmp_path, monkeypatch):
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


JOB_TABLES = {"jobs", "job_sources", "sources", "collect_runs"}


def _tables(conn):
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _open_as_is(path):
    """Open a database file without the version check or upgrade that connect does."""
    return closing(db._connect(path, db._raw_key(get_secret(db.KEY_NAME))))


@pytest.fixture
def v1_data_dir(tmp_path, monkeypatch):
    """A data folder set up by Phase 1, before the job tables existed."""
    path = tmp_path / "data"
    with monkeypatch.context() as phase_1:
        phase_1.setattr(db, "UPGRADES", db.UPGRADES[:1])
        phase_1.setattr(db, "SCHEMA_VERSION", 1)
        db.setup(path, PASSWORD)
    return path


def test_new_setup_has_the_job_tables_and_no_backup(data_dir):
    with closing(db.connect(data_dir)) as conn:
        assert JOB_TABLES <= _tables(conn)
    assert {path.name for path in data_dir.iterdir()} == {db.DB_FILE, db.RECOVERY_FILE}


def test_phase_1_database_is_backed_up_then_upgraded(v1_data_dir):
    backup = v1_data_dir / f"{db.DB_FILE}.bak-v1"
    backup.write_bytes(b"left by an earlier failed upgrade")
    with closing(db.connect(v1_data_dir)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert JOB_TABLES <= _tables(conn)
    with _open_as_is(backup) as conn:  # still encrypted with the same key
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert {path.name for path in v1_data_dir.iterdir()} == {db.DB_FILE, db.RECOVERY_FILE, backup.name}


def test_version_2_database_gets_the_board_columns_after_a_backup(tmp_path, monkeypatch):
    path = tmp_path / "data"
    with monkeypatch.context() as phase_2:
        phase_2.setattr(db, "UPGRADES", db.UPGRADES[:2])
        phase_2.setattr(db, "SCHEMA_VERSION", 2)
        db.setup(path, PASSWORD)
    with closing(db.connect(path)) as conn:
        assert {"misses", "retired_on"} <= {row[1] for row in conn.execute("PRAGMA table_info(sources)")}
    with _open_as_is(path / f"{db.DB_FILE}.bak-v2") as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_version_3_keeps_saved_jobs_and_allows_not_us(tmp_path, monkeypatch):
    path = tmp_path / "data"
    with monkeypatch.context() as phase_2:
        phase_2.setattr(db, "UPGRADES", db.UPGRADES[:2])
        phase_2.setattr(db, "SCHEMA_VERSION", 2)
        db.setup(path, PASSWORD)
        with closing(db.connect(path)) as conn, conn:
            conn.execute(
                "INSERT INTO jobs (apply_url, match_key, company, title, locations, us_location, remote, level, "
                "sponsorship, terms, first_seen, last_seen, status) VALUES ('https://acme.example/1', 'k', 'Acme', "
                "'ML Intern', '[]', 'yes', 'unknown', 'intern', 'unknown', '[]', 'now', 'now', 'open')"
            )
    with closing(db.connect(path)) as conn:
        assert conn.execute("SELECT company, title, us_location FROM jobs").fetchall() == [("Acme", "ML Intern", "yes")]
        conn.execute("UPDATE jobs SET us_location = 'no'")
        with pytest.raises(sqlcipher3.IntegrityError):
            conn.execute("UPDATE jobs SET us_location = 'maybe'")
        assert "jobs_match_key" in {row[1] for row in conn.execute("PRAGMA index_list(jobs)")}


def test_version_4_never_hides_a_job_and_marks_your_own(tmp_path, monkeypatch):
    path = tmp_path / "data"
    with monkeypatch.context() as phase_3:
        phase_3.setattr(db, "UPGRADES", db.UPGRADES[:3])
        phase_3.setattr(db, "SCHEMA_VERSION", 3)
        db.setup(path, PASSWORD)
        with closing(db.connect(path)) as conn, conn:
            for number, (us, status) in enumerate([("no", "open"), ("yes", "closed"), ("yes", "open")], 1):
                conn.execute(
                    "INSERT INTO jobs (id, apply_url, match_key, company, title, locations, us_location, remote, "
                    "level, sponsorship, terms, first_seen, last_seen, status) VALUES (?, ?, 'k', 'Acme', 'ML', "
                    "'[]', ?, 'unknown', 'intern', 'unknown', '[]', 'now', 'now', ?)",
                    (number, f"https://acme.example/{number}", us, status),
                )
            conn.execute("INSERT INTO job_sources VALUES ('manual', 'https://acme.example/3', 3, 1)")
    with closing(db.connect(path)) as conn:
        rows = conn.execute("SELECT us_location, status, added_by_you, check_location FROM jobs ORDER BY id")
        assert rows.fetchall() == [
            ("unclear", "open", 0, 1),  # was hidden as 'no': visible again, to be checked
            ("yes", "no longer available", 0, 0),  # 'no longer listed' in version 4
            ("yes", "open", 1, 0),  # added with `add` (another site)
        ]
        conn.execute("UPDATE jobs SET status = 'closed'")  # still allowed, for a later phase
        with pytest.raises(sqlcipher3.IntegrityError):
            conn.execute("UPDATE jobs SET status = 'deleted'")
    with _open_as_is(path / f"{db.DB_FILE}.bak-v3") as conn:  # SQLite's own copy, still encrypted
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 3
    with closing(sqlite3.connect(path / f"{db.DB_FILE}.bak-v3")) as plain, pytest.raises(sqlite3.DatabaseError):
        plain.execute("SELECT count(*) FROM sqlite_master")


def test_version_5_gives_each_job_that_is_not_open_a_reason(tmp_path, monkeypatch):
    path = tmp_path / "data"
    with monkeypatch.context() as phase_2:
        phase_2.setattr(db, "UPGRADES", db.UPGRADES[:4])
        phase_2.setattr(db, "SCHEMA_VERSION", 4)
        db.setup(path, PASSWORD)
        with closing(db.connect(path)) as conn, conn:
            for number, status in enumerate(["open", "no longer listed", "no longer listed"], 1):
                conn.execute(
                    "INSERT INTO jobs (id, apply_url, match_key, company, title, locations, us_location, remote, "
                    "level, sponsorship, terms, first_seen, last_seen, status) VALUES (?, ?, 'k', 'Acme', 'ML', "
                    "'[]', 'yes', 'unknown', 'intern', 'unknown', '[]', 'now', 'now', ?)",
                    (number, f"https://acme.example/{number}", status),
                )
            conn.execute("INSERT INTO sources (name, retired_on) VALUES ('greenhouse:gone', '2026-10-20')")
            conn.executemany(
                "INSERT INTO job_sources VALUES (?, ?, ?, ?)",
                [("greenhouse:acme", "1", 1, 1), ("greenhouse:gone", "2", 2, 0), ("simplify:List", "3", 3, 0)],
            )
    with closing(db.connect(path)) as conn:
        assert conn.execute("SELECT status, status_reason FROM jobs ORDER BY id").fetchall() == [
            ("open", None),
            ("no longer available", "board shut down"),
            ("no longer available", "reason not recorded (before 2026-10-21)"),
        ]
        assert conn.execute("SELECT unlisted_on, unlisted_reason FROM job_sources ORDER BY job_id").fetchall() == [
            (None, None),
            (None, "board shut down"),
            (None, "reason not recorded (before 2026-10-21)"),
        ]
        assert conn.execute("SELECT empty_runs FROM sources").fetchall() == [(0,)]
        with pytest.raises(sqlcipher3.IntegrityError):
            conn.execute("UPDATE jobs SET status = 'no longer listed'")
        with pytest.raises(sqlcipher3.IntegrityError):
            conn.execute("UPDATE jobs SET status_reason = 'gone'")
        assert "jobs_match_key" in {row[1] for row in conn.execute("PRAGMA index_list(jobs)")}
    with _open_as_is(path / f"{db.DB_FILE}.bak-v4") as conn:
        assert conn.execute("SELECT count(*) FROM jobs WHERE status = 'no longer listed'").fetchone()[0] == 2


def test_version_6_adds_the_dashboard_marks_and_keeps_jobs(tmp_path, monkeypatch):
    path = tmp_path / "data"
    with monkeypatch.context() as version_5:
        version_5.setattr(db, "UPGRADES", db.UPGRADES[:5])
        version_5.setattr(db, "SCHEMA_VERSION", 5)
        db.setup(path, PASSWORD)
        with closing(db.connect(path)) as conn, conn:
            conn.execute(
                "INSERT INTO jobs (apply_url, match_key, company, title, locations, us_location, remote, level, "
                "sponsorship, terms, first_seen, last_seen, status, check_location) VALUES ('https://acme.example/1', "
                "'k', 'Acme', 'ML', '[]', 'unclear', 'unknown', 'intern', 'unknown', '[]', 'now', 'now', 'open', 1)"
            )
    with closing(db.connect(path)) as conn:
        assert conn.execute(
            "SELECT company, us_location, check_location, not_interested, location_by_you FROM jobs"
        ).fetchall() == [("Acme", "unclear", 1, 0, 0)]
        conn.execute("INSERT INTO dashboard_login VALUES (1, 'scrypt$fake', 'now')")
        with pytest.raises(sqlcipher3.IntegrityError):
            conn.execute("INSERT INTO dashboard_login VALUES (2, 'scrypt$other', 'now')")
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT id, title, company, locations FROM jobs "
            "WHERE not_interested = 0 AND status = 'open' ORDER BY date_posted DESC"
        ).fetchall()
        assert "COVERING INDEX jobs_list" in str(plan)  # the list never reads the long descriptions
    with _open_as_is(path / f"{db.DB_FILE}.bak-v5") as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5


def test_version_7_lets_you_stop_reading_a_board(tmp_path, monkeypatch):
    path = tmp_path / "data"
    with monkeypatch.context() as version_6:
        version_6.setattr(db, "UPGRADES", db.UPGRADES[:6])
        version_6.setattr(db, "SCHEMA_VERSION", 6)
        db.setup(path, PASSWORD)
        with closing(db.connect(path)) as conn, conn:
            conn.execute("INSERT INTO sources (name) VALUES ('greenhouse:acme')")
    with closing(db.connect(path)) as conn:
        assert conn.execute("SELECT name, stopped_on FROM sources").fetchall() == [("greenhouse:acme", None)]
    with _open_as_is(path / f"{db.DB_FILE}.bak-v6") as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 6


def test_version_8_adds_the_phone_book_and_keeps_every_board(tmp_path, monkeypatch):
    path = tmp_path / "data"
    with monkeypatch.context() as version_7:
        version_7.setattr(db, "UPGRADES", db.UPGRADES[:7])
        version_7.setattr(db, "SCHEMA_VERSION", 7)
        db.setup(path, PASSWORD)
        with closing(db.connect(path)) as conn, conn:
            conn.execute("INSERT INTO sources (name) VALUES ('greenhouse:acme')")
    with closing(db.connect(path)) as conn:
        assert conn.execute("SELECT name, found_by, company FROM sources").fetchall() == [
            ("greenhouse:acme", None, None)
        ]
        for table in ("phonebook", "phonebook_done", "phonebook_runs"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0  # noqa: S608 (fixed names)
    with _open_as_is(path / f"{db.DB_FILE}.bak-v7") as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7


def test_version_9_adds_scoring_and_keeps_every_job(tmp_path, monkeypatch):
    path = tmp_path / "data"
    with monkeypatch.context() as version_8:
        version_8.setattr(db, "UPGRADES", db.UPGRADES[:8])
        version_8.setattr(db, "SCHEMA_VERSION", 8)
        db.setup(path, PASSWORD)
        with closing(db.connect(path)) as conn, conn:
            conn.execute(
                "INSERT INTO jobs (id, apply_url, match_key, company, title, locations, us_location, remote, level, "
                "sponsorship, terms, description, first_seen, last_seen, status) VALUES (1, 'https://acme.example/1', "
                "'k', 'Acme', 'ML Intern', '[]', 'yes', 'unknown', 'intern', 'unknown', '[]', 'Train robots.', 'now', "
                "'now', 'open')"
            )
    with closing(db.connect(path)) as conn:
        assert conn.execute("SELECT title, prepare_anyway FROM jobs").fetchall() == [("ML Intern", 0)]
        for table in ("stage_a", "ai_scores", "answer_key", "scoring_days", "ai_failures"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0  # noqa: S608 (fixed names)
        trigger = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'job_text_changed'").fetchone()[0]
        assert "UPDATE ai_failures SET changed = 1" in trigger
    with _open_as_is(path / f"{db.DB_FILE}.bak-v8") as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8


def _scored_job(conn) -> None:
    """One job with a quick check and an AI score, both up to date."""
    conn.execute(
        "INSERT INTO jobs (id, apply_url, match_key, company, title, locations, us_location, remote, level, "
        "sponsorship, terms, description, first_seen, last_seen, status) VALUES (1, 'https://acme.example/1', 'k', "
        "'Acme', 'ML Intern', '[]', 'yes', 'unknown', 'intern', 'unknown', '[]', 'Train robots.', 'now', 'now', 'open')"
    )
    conn.execute(
        "INSERT INTO stage_a (job_id, setup, keywords, job_skills, shared_skills, meaning, title_only, passed, "
        "checked_on) VALUES (1, 'fp', '[\"ML\"]', 4, '[\"Python\"]', 0.8, 0, 1, 'now')"
    )
    conn.execute("INSERT INTO ai_scores (job_id, model, score, resume, scored_on) VALUES (1, 'm', 80, 'fp', 'now')")


FAILED_TRY = "INSERT INTO ai_failures (job_id, model, failed, resume, tried_on) VALUES (1, 'm', 'x', 'fp2', 'now')"


def test_version_10_adds_failed_new_tries_and_keeps_every_score(tmp_path, monkeypatch):
    path = tmp_path / "data"
    with monkeypatch.context() as version_9:
        version_9.setattr(db, "UPGRADES", db.UPGRADES[:9])
        version_9.setattr(db, "SCHEMA_VERSION", 9)
        db.setup(path, PASSWORD)
        with closing(db.connect(path)) as conn, conn:
            _scored_job(conn)
    with closing(db.connect(path)) as conn:
        assert conn.execute("SELECT score FROM ai_scores").fetchall() == [(80,)]
        assert conn.execute("SELECT count(*) FROM ai_failures").fetchone()[0] == 0
        with conn:
            conn.execute(FAILED_TRY)
            conn.execute("UPDATE jobs SET description = 'Train more robots.' WHERE id = 1")
        assert conn.execute("SELECT changed FROM ai_failures").fetchone()[0] == 1  # the trigger covers it too
    with _open_as_is(path / f"{db.DB_FILE}.bak-v9") as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 9


def test_version_11_adds_documents_and_keeps_every_job(tmp_path, monkeypatch):
    path = tmp_path / "data"
    with monkeypatch.context() as version_10:
        version_10.setattr(db, "UPGRADES", db.UPGRADES[:10])
        version_10.setattr(db, "SCHEMA_VERSION", 10)
        db.setup(path, PASSWORD)
        with closing(db.connect(path)) as conn, conn:
            _scored_job(conn)
    with closing(db.connect(path)) as conn:
        assert conn.execute("SELECT title, prepare_requested_on FROM jobs").fetchall() == [("ML Intern", None)]
        assert conn.execute("SELECT score FROM ai_scores").fetchall() == [(80,)]
        for table in ("questionnaire", "tile_sets", "tiles"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0  # noqa: S608 (fixed names)
    with _open_as_is(path / f"{db.DB_FILE}.bak-v10") as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 10


def test_version_12_adds_uploaded_resumes_and_keeps_confirmed_tiles(tmp_path, monkeypatch):
    path = tmp_path / "data"
    with monkeypatch.context() as version_11:
        version_11.setattr(db, "UPGRADES", db.UPGRADES[:11])
        version_11.setattr(db, "SCHEMA_VERSION", 11)
        db.setup(path, PASSWORD)
        with closing(db.connect(path)) as conn, conn:
            _scored_job(conn)
            conn.execute("INSERT INTO tile_sets (id, resume, confirmed_on) VALUES (1, 'fp', 'now')")
    with closing(db.connect(path)) as conn:
        assert conn.execute("SELECT id, file_id FROM tile_sets").fetchall() == [(1, None)]  # the Resume Profile's
        assert conn.execute("SELECT writer_sources FROM jobs").fetchall() == [(None,)]
        assert conn.execute("SELECT count(*) FROM resume_files").fetchone()[0] == 0
        with pytest.raises(sqlcipher3.IntegrityError):
            conn.execute("INSERT INTO writer_sources VALUES (2, '[\"profile\"]')")  # one default only
    with _open_as_is(path / f"{db.DB_FILE}.bak-v11") as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 11


def test_version_13_adds_preparations_and_steps_and_keeps_every_job(tmp_path, monkeypatch):
    path = tmp_path / "data"
    with monkeypatch.context() as version_12:
        version_12.setattr(db, "UPGRADES", db.UPGRADES[:12])
        version_12.setattr(db, "SCHEMA_VERSION", 12)
        db.setup(path, PASSWORD)
        with closing(db.connect(path)) as conn, conn:
            _scored_job(conn)
    with closing(db.connect(path)) as conn:
        assert conn.execute("SELECT title FROM jobs").fetchall() == [("ML Intern",)]
        for table in ("preparations", "steps"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0  # noqa: S608 (fixed names)
    with _open_as_is(path / f"{db.DB_FILE}.bak-v12") as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 12


def test_version_14_adds_failed_tries_and_keeps_every_step(tmp_path, monkeypatch):
    path = tmp_path / "data"
    with monkeypatch.context() as version_13:
        version_13.setattr(db, "UPGRADES", db.UPGRADES[:13])
        version_13.setattr(db, "SCHEMA_VERSION", 13)
        db.setup(path, PASSWORD)
        with closing(db.connect(path)) as conn, conn:
            _scored_job(conn)
            conn.execute(PREPARATION.format(1, "resume", 1, "writing", "NULL"))
            conn.execute(STEP.format("writer", "failed", 0))
    with closing(db.connect(path)) as conn:
        assert conn.execute("SELECT status FROM steps").fetchall() == [("failed",)]
        assert conn.execute("SELECT count(*) FROM failed_tries").fetchone()[0] == 0  # none were kept before
        with pytest.raises(sqlcipher3.IntegrityError):
            conn.execute("INSERT INTO failed_tries (step_id, try_number, reason, saved_on) VALUES (1, 0, 'x', 'now')")
    with _open_as_is(path / f"{db.DB_FILE}.bak-v13") as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 13


def test_version_15_rebuilds_versions_and_steps_for_the_council_and_keeps_every_row(tmp_path, monkeypatch):
    path = tmp_path / "data"
    with monkeypatch.context() as version_14:
        version_14.setattr(db, "UPGRADES", db.UPGRADES[:14])
        version_14.setattr(db, "SCHEMA_VERSION", 14)
        db.setup(path, PASSWORD)
        with closing(db.connect(path)) as conn, conn:
            _scored_job(conn)
            conn.execute(PREPARATION.format(1, "resume", 1, "done", "'{}'"))
            conn.execute(STEP.format("writer", "done", 0))
            conn.execute("INSERT INTO failed_tries (step_id, try_number, reason, saved_on) VALUES (1, 1, 'x', 'now')")
    with closing(db.connect(path)) as conn:
        assert conn.execute(
            "SELECT kind, status, draft, mode, round, council, agreed, asked, waived, automatic FROM preparations"
        ).fetchall() == [("resume", "done", "{}", "normal", 1, None, None, "[]", "[]", 0)]  # Part 4b: normal price
        assert conn.execute("SELECT member, turn, mode, status, tries, waits, batch_id FROM steps").fetchall() == [
            ("writer", 0, "direct", "done", 1, 0, None)
        ]
        assert conn.execute("SELECT count(*) FROM failed_tries JOIN steps ON steps.id = step_id").fetchone()[0] == 1
        assert "steps_day" in {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        with conn:  # each pilot line-up writes its own version of the same job at the same time
            conn.execute(
                "INSERT INTO pilots (id, jobs, planted, rounds, turns, created_on) VALUES (1, '[1]', '{}', 3, 1, 'now')"
            )
            for number, lineup in enumerate(("laptop", "high"), 2):
                conn.execute(PILOT.format(number, number - 1, lineup))
        with pytest.raises(sqlcipher3.IntegrityError):
            conn.execute(PILOT.format(4, 3, "laptop"))  # but one line-up writes one at a time
    with _open_as_is(path / f"{db.DB_FILE}.bak-v14") as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 14


def test_version_16_adds_the_tracker_and_keeps_jobs(tmp_path, monkeypatch):
    path = tmp_path / "data"
    with monkeypatch.context() as version_15:
        version_15.setattr(db, "UPGRADES", db.UPGRADES[:15])
        version_15.setattr(db, "SCHEMA_VERSION", 15)
        db.setup(path, PASSWORD)
        with closing(db.connect(path)) as conn, conn:
            _scored_job(conn)
    card = "INSERT OR REPLACE INTO tracker (job_id, stage, round, applied_on, changed_on) VALUES (1, {}, 'now')"
    move = "INSERT INTO tracker_moves (job_id, from_stage, to_stage, moved_by, moved_on) VALUES (1, 'ready', {}, 'now')"
    with closing(db.connect(path)) as conn:
        assert conn.execute("SELECT company FROM jobs").fetchall() == [("Acme",)]
        with conn:
            conn.execute(card.format("'applied', NULL, '2026-11-04'"))
            conn.execute(card.format("'interview', '2', '2026-11-04'"))
            conn.execute(move.format("'applied', 'you'"))
        for bad in [
            card.format("'interview', NULL, '2026-11-04'"),
            card.format("'applied', '1', '2026-11-04'"),
            card.format("'ready', NULL, '2026-11-04'"),
            card.format("'applied', NULL, NULL"),
            card.format("'ghosted', NULL, '2026-11-04'"),
            move.format("'applied', 'robot'"),
            move.format("'ghosted', 'you'"),
        ]:
            with pytest.raises(sqlcipher3.IntegrityError):
                conn.execute(bad)
    with _open_as_is(path / f"{db.DB_FILE}.bak-v15") as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 15


def test_version_17_adds_the_gmail_tables_and_keeps_the_tracker(tmp_path, monkeypatch):
    path = tmp_path / "data"
    with monkeypatch.context() as version_16:
        version_16.setattr(db, "UPGRADES", db.UPGRADES[:16])
        version_16.setattr(db, "SCHEMA_VERSION", 16)
        db.setup(path, PASSWORD)
        with closing(db.connect(path)) as conn, conn:
            _scored_job(conn)
            conn.execute(
                "INSERT INTO tracker (job_id, stage, applied_on, changed_on) VALUES (1, 'applied', '2026-11-04', 'now')"
            )
    email = (
        "INSERT INTO emails (message_id, thread_id, received_on, sender_domain, label, labelled_by, to_check, subject) "
        "VALUES ({}, 't1', 'now', 'example.com', {}, {}, {}, {})"
    )
    with closing(db.connect(path)) as conn:
        assert conn.execute("SELECT stage FROM tracker").fetchall() == [("applied",)]
        with conn:
            conn.execute(email.format("'m1'", "'rejection'", "'rules'", 0, "NULL"))
            conn.execute(email.format("'m2'", "'unclear'", "'local model'", 1, "'Your application'"))
            conn.execute("INSERT INTO gmail_sync (id) VALUES (1)")
        for bad in [
            email.format("'m3'", "'offer'", "'rules'", 0, "'Kept without a check'"),
            email.format("'m4'", "'spam'", "'rules'", 0, "NULL"),
            email.format("'m5'", "'offer'", "'cloud'", 0, "NULL"),
            email.format("'m1'", "'offer'", "'you'", 0, "NULL"),
            "INSERT INTO gmail_sync (id) VALUES (2)",
        ]:
            with pytest.raises(sqlcipher3.IntegrityError):
                conn.execute(bad)
    with _open_as_is(path / f"{db.DB_FILE}.bak-v16") as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 16


def _everything(conn) -> tuple[list, dict]:
    """Every table, index and trigger as defined, and every table's rows, but for the tables the export added."""
    found = conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").fetchall()
    found = [row for row in found if row[2] not in EXPORT_TABLES]  # an index of theirs too
    tables = [row[1] for row in found if row[0] == "table" and not row[1].startswith("sqlite_")]
    return found, {name: conn.execute(f'SELECT * FROM "{name}"').fetchall() for name in tables}  # noqa: S608


def _export_tables(conn) -> list:
    names = ", ".join(f"'{name}'" for name in EXPORT_TABLES)
    return conn.execute(f"SELECT name, sql FROM sqlite_master WHERE name IN ({names}) ORDER BY name").fetchall()  # noqa: S608


def test_version_22_adds_paired_browsers_and_changes_nothing_else(tmp_path, monkeypatch):
    path = tmp_path / "upgraded"
    with monkeypatch.context() as version_21:
        version_21.setattr(db, "UPGRADES", db.UPGRADES[:21])
        version_21.setattr(db, "SCHEMA_VERSION", 21)
        db.setup(path, PASSWORD)
        with closing(db.connect(path)) as conn:
            with conn:
                _scored_job(conn)
            before = _everything(conn)
    with monkeypatch.context() as version_22:
        version_22.setattr(db, "UPGRADES", db.UPGRADES[:22])
        version_22.setattr(db, "SCHEMA_VERSION", 22)
        with closing(db.connect(path)) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 22
            found, rows = _everything(conn)
            added = {row[1] for row in found if row[2] in ("paired_browsers", "sqlite_sequence")}
            assert added == {
                "paired_browsers", "sqlite_sequence",  # SQLite's own record for AUTOINCREMENT, the first such table
                "sqlite_autoindex_paired_browsers_1", "sqlite_autoindex_paired_browsers_2",
            }  # fmt: skip
            assert [row for row in found if row[1] not in added] == before[0]
            assert {name: rows[name] for name in before[1]} == before[1] and len(rows) == len(before[1]) + 1
            assert conn.execute("SELECT count(*) FROM paired_browsers").fetchone()[0] == 0
            add = "INSERT INTO paired_browsers (name, key_hash, added_on) VALUES (?, ?, 'then')"
            with conn:
                conn.execute(add, ("Laptop", "hash-1"))
            for repeated in (("Laptop", "hash-2"), ("Other", "hash-1")):
                with pytest.raises(sqlcipher3.IntegrityError):
                    conn.execute(add, repeated)
        with closing(db.connect(path)) as conn:  # runs once: opening again changes nothing
            assert conn.execute("SELECT name FROM paired_browsers").fetchall() == [("Laptop",)]


def test_version_23_adds_fills_and_changes_nothing_else(tmp_path, monkeypatch):
    path = tmp_path / "upgraded"
    with monkeypatch.context() as version_22:
        version_22.setattr(db, "UPGRADES", db.UPGRADES[:22])
        version_22.setattr(db, "SCHEMA_VERSION", 22)
        db.setup(path, PASSWORD)
        with closing(db.connect(path)) as conn:
            with conn:
                _scored_job(conn)
            before = _everything(conn)
    with monkeypatch.context() as version_23:
        version_23.setattr(db, "UPGRADES", db.UPGRADES[:23])
        version_23.setattr(db, "SCHEMA_VERSION", 23)
        with closing(db.connect(path)) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 23
            found, rows = _everything(conn)
            added = {row[1] for row in found if row[2] == "fills"}
            assert added == {"fills", "fills_started"}
            assert [row for row in found if row[1] not in added] == before[0]
            assert {name: rows[name] for name in before[1]} == before[1] and len(rows) == len(before[1]) + 1
            assert conn.execute("SELECT count(*) FROM fills").fetchone()[0] == 0
            with conn:
                conn.execute("INSERT INTO fills (job_id, address, started_on, toolbar, state) "
                             "VALUES (1, 'x', 'then', 0, 'waiting')")  # fmt: skip
        with closing(db.connect(path)) as conn:  # runs once: opening again changes nothing
            assert conn.execute("SELECT count(*) FROM fills").fetchone()[0] == 1


def test_version_24_adds_two_columns_to_fills_and_changes_nothing_else(tmp_path, monkeypatch):
    path = tmp_path / "upgraded"
    with monkeypatch.context() as version_23:
        version_23.setattr(db, "UPGRADES", db.UPGRADES[:23])
        version_23.setattr(db, "SCHEMA_VERSION", 23)
        db.setup(path, PASSWORD)
        with closing(db.connect(path)) as conn:
            with conn:
                _scored_job(conn)
                conn.execute("INSERT INTO fills (job_id, address, started_on, toolbar, state) "
                             "VALUES (1, 'x', 'then', 0, 'sent')")  # fmt: skip
            before = _everything(conn)
    with monkeypatch.context() as version_24:
        version_24.setattr(db, "UPGRADES", db.UPGRADES[:24])
        version_24.setattr(db, "SCHEMA_VERSION", 24)
        with closing(db.connect(path)) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 24
            found, rows = _everything(conn)
            assert [row for row in found if row[1] != "fills"] == [row for row in before[0] if row[1] != "fills"]
            assert {name: rows[name] for name in rows if name != "fills"} == {
                name: before[1][name] for name in before[1] if name != "fills"
            }
            columns = [row[1] for row in conn.execute("PRAGMA table_info(fills)")]
            assert columns[-2:] == ["filled", "files"] and len(columns) == 11
            assert rows["fills"] == [(*before[1]["fills"][0], None, None)]  # an old row keeps all it had
            with conn:
                conn.execute("UPDATE fills SET filled = '[]', files = '[]'")
        with closing(db.connect(path)) as conn:  # runs once: opening again changes nothing
            assert conn.execute("SELECT filled, files FROM fills").fetchall() == [("[]", "[]")]


def test_version_25_adds_six_columns_and_changes_nothing_else(tmp_path, monkeypatch):
    path = tmp_path / "upgraded"
    changed = ("fills", "submissions")
    with monkeypatch.context() as version_24:
        version_24.setattr(db, "UPGRADES", db.UPGRADES[:24])
        version_24.setattr(db, "SCHEMA_VERSION", 24)
        db.setup(path, PASSWORD)
        with closing(db.connect(path)) as conn:
            with conn:
                _scored_job(conn)
                conn.execute("INSERT INTO fills (job_id, address, started_on, toolbar, state) "
                             "VALUES (1, 'x', 'then', 0, 'sent')")  # fmt: skip
                conn.execute("INSERT INTO submissions (job_id, submitted_on, saved_on, how, address, fields, files) "
                             "VALUES (1, '2026-11-05', 'then', 'by hand', 'x', '[]', '[]')")  # fmt: skip
            before = _everything(conn)
            old = {name: [row[1] for row in conn.execute(f"PRAGMA table_info({name})")] for name in changed}
    with monkeypatch.context() as version_25:
        version_25.setattr(db, "UPGRADES", db.UPGRADES[:25])
        version_25.setattr(db, "SCHEMA_VERSION", 25)
        with closing(db.connect(path)) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 25
            found, rows = _everything(conn)
            assert [row for row in found if row[1] not in changed] == [
                row for row in before[0] if row[1] not in changed
            ]
            assert {name: rows[name] for name in rows if name not in changed} == {
                name: before[1][name] for name in before[1] if name not in changed
            }
            fills = [row[1] for row in conn.execute("PRAGMA table_info(fills)")]
            submissions = [row[1] for row in conn.execute("PRAGMA table_info(submissions)")]
            assert fills == [*old["fills"], "taken_on", "checked_on", "values_read", "clicked_on", "pdfs"]
            assert submissions == [*old["submissions"], "checked_on"]
            assert rows["fills"] == [
                (*before[1]["fills"][0], None, None, None, None, None)
            ]  # an old row keeps all it had
            assert rows["submissions"] == [(*before[1]["submissions"][0], None)]
            with conn:
                conn.execute("UPDATE fills SET values_read = '{}'")
        with closing(db.connect(path)) as conn:  # runs once: opening again changes nothing
            assert conn.execute("SELECT values_read FROM fills").fetchall() == [("{}",)]


def test_version_20_adds_the_export_tables_and_changes_nothing_else(tmp_path, monkeypatch):
    path = tmp_path / "upgraded"
    with monkeypatch.context() as version_19:
        version_19.setattr(db, "UPGRADES", db.UPGRADES[:19])
        version_19.setattr(db, "SCHEMA_VERSION", 19)
        db.setup(path, PASSWORD)
        with closing(db.connect(path)) as conn:
            with conn:
                _scored_job(conn)
                conn.execute("INSERT INTO tracker VALUES (1, 'applied', NULL, '2026-10-17', 'then')")
                conn.execute("INSERT INTO tracker_moves VALUES (1, 1, 'ready', 'applied', NULL, 'you', 'then')")
                conn.execute("INSERT INTO submissions VALUES (1, 1, '2026-10-17', 'then', 'by hand', "
                             "'https://acme.example/1', '[]', '[]')")  # fmt: skip
            before = _everything(conn)
    with monkeypatch.context() as version_20:
        version_20.setattr(db, "UPGRADES", db.UPGRADES[:20])
        version_20.setattr(db, "SCHEMA_VERSION", 20)
        with closing(db.connect(path)) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 20
            assert _everything(conn) == before
            upgraded = _export_tables(conn)
            assert conn.execute("SELECT count(*) FROM exported").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM export_runs").fetchone()[0] == 0
    assert [name for name, _ in upgraded] == sorted(EXPORT_TABLES)
    db.setup(tmp_path / "new", PASSWORD, replace_key=True)
    with closing(db.connect(tmp_path / "new")) as conn:
        assert _export_tables(conn) == upgraded  # a new database gets the very same tables


def test_the_export_tables_hold_what_they_should_and_nothing_else(conn):
    with conn:
        conn.execute("INSERT INTO export_runs (id, tried_on) VALUES (1, 'now')")
        conn.execute(
            "INSERT INTO exported (job_id, folder, exported_on) VALUES (7, '2026-10-17/Acme_ML-Engineer', 'now')"
        )
    for refused in (
        "INSERT INTO export_runs (id) VALUES (2)",  # one row only
        "INSERT INTO export_runs (id) VALUES (0)",
        # one folder per job, one job per folder
        "INSERT INTO exported (job_id, folder, exported_on) VALUES (7, '2026-10-17/Acme_ML-Engineer_2', 'now')",
        "INSERT INTO exported (job_id, folder, exported_on) VALUES (8, '2026-10-17/Acme_ML-Engineer', 'now')",
        "INSERT INTO exported (job_id, folder, exported_on) VALUES (8, NULL, 'now')",
        "INSERT INTO exported (job_id, folder, exported_on) VALUES (8, 'folder', NULL)",
    ):
        with pytest.raises(sqlcipher3.IntegrityError):
            conn.execute(refused)
    assert conn.execute("SELECT id, tried_on, good_on, last_result FROM export_runs").fetchall() == [
        (1, "now", None, None)
    ]


def test_a_database_already_up_to_date_opens_with_no_change(data_dir):
    with closing(db.connect(data_dir)) as conn, conn:
        conn.execute(
            "INSERT INTO exported (job_id, folder, exported_on) VALUES (7, '2026-10-17/Acme_ML-Engineer', 'now')"
        )
    before = (data_dir / db.DB_FILE).read_bytes()
    with closing(db.connect(data_dir)) as conn:
        assert conn.execute("SELECT folder FROM exported").fetchall() == [("2026-10-17/Acme_ML-Engineer",)]
    assert (data_dir / db.DB_FILE).read_bytes() == before
    assert {path.name for path in data_dir.iterdir()} == {db.DB_FILE, db.RECOVERY_FILE}  # no backup made


def test_version_19_adds_the_wording_of_each_answer_and_keeps_your_words(tmp_path, monkeypatch):
    path = tmp_path / "data"
    with monkeypatch.context() as version_18:
        version_18.setattr(db, "UPGRADES", db.UPGRADES[:18])
        version_18.setattr(db, "SCHEMA_VERSION", 18)
        db.setup(path, PASSWORD)
        with closing(db.connect(path)) as conn, conn:
            _scored_job(conn)
            conn.execute("INSERT INTO form_answers VALUES (1, 'question_1', 'My words', 'you', NULL, 'then')")
    with closing(db.connect(path)) as conn:
        assert conn.execute("SELECT * FROM form_answers").fetchall() == [
            (1, "question_1", "My words", "you", None, "then", ""),  # '' counts as changed: asked again, never deleted
        ]


def test_version_18_adds_the_form_tables_and_keeps_every_preparation(tmp_path, monkeypatch):
    path = tmp_path / "data"
    every_row = ("SELECT * FROM preparations ORDER BY id", "SELECT * FROM steps", "SELECT * FROM failed_tries")
    with monkeypatch.context() as version_17:
        version_17.setattr(db, "UPGRADES", db.UPGRADES[:17])
        version_17.setattr(db, "SCHEMA_VERSION", 17)
        db.setup(path, PASSWORD)
        with closing(db.connect(path)) as conn, conn:
            _scored_job(conn)
            conn.execute(PREPARATION.format(1, "resume", 1, "done", "'{}'"))
            conn.execute(STEP.format("writer", "done", 0))
            conn.execute("INSERT INTO failed_tries (step_id, try_number, reason, saved_on) VALUES (1, 1, 'x', 'now')")
            conn.execute(PREPARATION.format(2, "cover_letter", 1, "stopped", "NULL"))
            conn.execute(
                "INSERT INTO pilots (id, jobs, planted, rounds, turns, created_on) VALUES (1, '[1]', '{}', 3, 1, 'now')"
            )
            conn.execute(PILOT.format(3, 1, "laptop"))
            before = [conn.execute(query).fetchall() for query in every_row]
    form = "INSERT INTO forms (job_id, read_on, read_from, fields, fingerprint) VALUES ({}, 'now', {}, '[]', 'fp')"
    answer = (
        "INSERT INTO form_answers (job_id, field, answer, given_by, preparation_id, saved_on) "
        "VALUES (1, {}, 'Yes', {}, {}, 'now')"
    )
    sent = (
        "INSERT INTO submissions (job_id, submitted_on, saved_on, how, address, fields, files) "
        "VALUES ({}, '2026-11-05', 'now', {}, 'https://jobs.example/1/apply', '[]', '[]')"
    )
    with closing(db.connect(path)) as conn:
        assert [conn.execute(query).fetchall() for query in every_row] == before
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        with conn:
            conn.execute(PREPARATION.format(4, "answers", 1, "writing", "NULL"))
            conn.execute(form.format(1, "'api'"))
            conn.execute(answer.format("'question_1'", "'you'", "NULL"))
            conn.execute(answer.format("'question_2'", "'writer'", 4))
            conn.execute(sent.format(1, "'by hand'"))
        for bad in [
            PREPARATION.format(5, "form", 1, "writing", "NULL"),
            PREPARATION.format(5, "answers", 2, "writing", "NULL"),  # a second version written at the same time
            form.format(2, "'cloud'"),
            answer.format("'question_3'", "'writer'", "NULL"),  # the Writer's answer without its version
            answer.format("'question_3'", "'you'", 4),  # your answer tied to a Writer's version
            sent.format(1, "'browser'"),  # a second record for the same job
            sent.format(2, "'robot'"),
        ]:
            with pytest.raises(sqlcipher3.IntegrityError):
                conn.execute(bad)
    with _open_as_is(path / f"{db.DB_FILE}.bak-v17") as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 17


PILOT = (
    "INSERT INTO preparations (id, job_id, kind, version, sources, writer, status, pilot_id, lineup, created_on) "
    "VALUES ({}, 1, 'pilot', {}, '[]', '{{}}', 'writing', 1, '{}', 'now')"
)
COUNCIL_STEP = (
    "INSERT INTO steps (preparation_id, member, turn, mode, service, model, status, reserved, retry_after, batch_id, "
    "day, sent_on) VALUES (1, '{}', {}, '{}', 'anthropic', 'm', '{}', {}, {}, {}, '2026-11-01', 'now')"
)
VERSION = (
    "INSERT INTO preparations (id, job_id, kind, version, sources, writer, status, created_on, {}) "
    "VALUES (2, 1, 'cover_letter', 1, '[]', '{{}}', 'writing', 'now', {})"
)


PREPARATION = (
    "INSERT INTO preparations (id, job_id, kind, version, sources, writer, status, draft, created_on) "
    "VALUES ({}, 1, '{}', {}, '[]', '{{}}', '{}', {}, 'now')"
)
STEP = (
    "INSERT INTO steps (preparation_id, member, service, model, status, reserved, day, sent_on) "
    "VALUES (1, '{}', 'ollama', 'm', '{}', {}, '2026-10-30', 'now')"
)


@pytest.mark.parametrize(
    "sql",
    [
        PREPARATION.format(2, "resume", 1, "done", "'{}'"),  # the same job, kind and version
        PREPARATION.format(2, "resume", 2, "writing", "NULL"),  # a second version written at the same time
        PREPARATION.format(2, "letter", 1, "writing", "NULL"),
        PREPARATION.format(2, "cover_letter", 1, "done", "NULL"),  # done, but no draft
        PREPARATION.format(2, "cover_letter", 0, "writing", "NULL"),
        PREPARATION.format(2, "cover_letter", 1, "queued", "NULL"),
        STEP.format("writer", "sent", 0),  # the same preparation, round and member
        STEP.format("editor", "done", 0.5),  # a reservation left after the answer came
        STEP.format("editor", "planned", 0),
        STEP.format("judge", "done", 0),  # not a council member
        COUNCIL_STEP.format("writer", 1, "direct", "done", 0, "NULL", "NULL"),  # the Writer takes no discussion turn
        COUNCIL_STEP.format("truth", 4, "direct", "done", 0, "NULL", "NULL"),  # at most 3 turns
        COUNCIL_STEP.format("truth", 0, "cheap", "done", 0, "NULL", "NULL"),
        COUNCIL_STEP.format("truth", 0, "flex", "waiting", 0.2, "NULL", "NULL"),  # money set aside while not sent
        COUNCIL_STEP.format("truth", 0, "flex", "done", 0, "'later'", "NULL"),  # a wait after the answer came
        COUNCIL_STEP.format("truth", 0, "flex", "sent", 0.2, "NULL", "'b-1'"),  # a batch id without a batch
        "INSERT INTO preparations (id, job_id, kind, version, sources, writer, status, created_on) "
        "VALUES (2, 1, 'pilot', 1, '[]', '{}', 'writing', 'now')",  # a pilot version of no pilot or line-up
        VERSION.format("lineup", "'laptop'"),  # a line-up outside a pilot
        VERSION.format("round", 7),
        VERSION.format("mode", "'full'"),
        VERSION.format("agreed", 2),
        VERSION.format("wording", "'mine'"),
        VERSION.format("automatic", 2),
        "INSERT INTO objection_marks (step_id, objection, useful, marked_on) VALUES (1, 'TC-1', 2, 'now')",
        "INSERT INTO pilots (jobs, planted, rounds, turns, created_on) VALUES ('[]', '{}', 6, 1, 'now')",
        STEP.format("editor", "sent", -1),
    ],
)
def test_preparation_tables_refuse_impossible_values(data_dir, sql):
    with closing(db.connect(data_dir)) as conn:
        with conn:
            _scored_job(conn)
            conn.execute(PREPARATION.format(1, "resume", 1, "writing", "NULL"))
            conn.execute(STEP.format("writer", "sent", 0.3))
        with pytest.raises(sqlcipher3.IntegrityError):
            conn.execute(sql)


TILE = "INSERT INTO tiles (set_id, tile_id, position, kind, section, facts) VALUES (1, '{}', {}, '{}', 'Work', '{{}}')"


@pytest.mark.parametrize(
    "sql",
    [
        TILE.format("hob-1", 2, "hobby"),
        TILE.format("exp-2", 1, "experience"),  # the same place as the first tile
        "INSERT INTO questionnaire VALUES (2, '{}', 'now')",  # only one set of answers
    ],
)
def test_document_tables_refuse_impossible_values(data_dir, sql):
    with closing(db.connect(data_dir)) as conn:
        with conn:
            conn.execute("INSERT INTO tile_sets (id, resume, confirmed_on) VALUES (1, 'fp', 'now')")
            conn.execute(TILE.format("exp-1", 1, "experience"))
        with pytest.raises(sqlcipher3.IntegrityError):
            conn.execute(sql)


@pytest.mark.parametrize(
    ("update", "changed"),
    [
        ("description = 'Train more robots.'", 1),
        ("title = 'ML Engineer Intern'", 1),
        ("description = 'Train robots.', last_seen = 'later'", 0),  # a collection saving the same text
        ("prepare_anyway = 1", 0),
    ],
)
def test_changed_job_text_marks_its_results_out_of_date(data_dir, update, changed):
    with closing(db.connect(data_dir)) as conn, conn:
        _scored_job(conn)
        conn.execute(FAILED_TRY)
        conn.execute(f"UPDATE jobs SET {update} WHERE id = 1")  # noqa: S608 (fixed test values)
        assert conn.execute("SELECT changed FROM stage_a").fetchone()[0] == changed
        assert conn.execute("SELECT changed FROM ai_scores").fetchone()[0] == changed
        assert conn.execute("SELECT changed FROM ai_failures").fetchone()[0] == changed


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE ai_scores SET score = 101",
        "UPDATE ai_scores SET score = NULL",  # neither a score nor why it failed
        "UPDATE ai_scores SET failed = 'bad answer twice'",  # both a score and a failure
        "INSERT INTO answer_key VALUES (1, 'perhaps', 'now')",
    ],
)
def test_scoring_tables_refuse_impossible_values(data_dir, sql):
    with closing(db.connect(data_dir)) as conn:
        with conn:
            _scored_job(conn)
        with pytest.raises(sqlcipher3.IntegrityError):
            conn.execute(sql)


def test_the_database_uses_wal_so_readers_never_wait(data_dir):
    with closing(db.connect(data_dir)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    with _open_as_is(data_dir / db.DB_FILE) as conn:  # kept in the file itself
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_failed_upgrade_leaves_version_1_and_can_be_retried(v1_data_dir, monkeypatch):
    with monkeypatch.context() as broken:
        broken.setattr(db, "UPGRADES", ("", "CREATE TABLE half_done (x); not valid sql"))
        with pytest.raises(sqlcipher3.OperationalError):
            db.connect(v1_data_dir)
    with _open_as_is(v1_data_dir / db.DB_FILE) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert "half_done" not in _tables(conn)
    db.connect(v1_data_dir).close()


def test_failed_backup_stops_before_upgrading(v1_data_dir, monkeypatch):
    def disk_full(path, mode):
        assert path.name.endswith(".partial")  # the copy is already written when this fails
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(db.Path, "chmod", disk_full)
    with pytest.raises(OSError):
        db.connect(v1_data_dir)
    assert {path.name for path in v1_data_dir.iterdir()} == {db.DB_FILE, db.RECOVERY_FILE}
    with _open_as_is(v1_data_dir / db.DB_FILE) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_database_from_a_newer_app_is_refused_untouched(data_dir):
    with _open_as_is(data_dir / db.DB_FILE) as conn:
        conn.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
    before = (data_dir / db.DB_FILE).read_bytes()
    with pytest.raises(db.DatabaseError, match="newer version of auto_apply"):
        db.connect(data_dir)
    assert (data_dir / db.DB_FILE).read_bytes() == before


@pytest.mark.parametrize(
    ("error", "line"),
    [
        (PermissionError(13, "Permission denied", "/home/someone/data/app.db"), "Error: Permission denied: app.db"),
        (
            sqlcipher3.OperationalError("disk I/O error"),
            "Error: the database could not be read or written (disk I/O error).",
        ),
        (KeyboardInterrupt(), "\nCancelled."),
        (db.DatabaseError("Database key not found."), "Error: Database key not found."),
    ],
)
def test_error_line_never_shows_a_full_path(error, line):
    assert db.error_line(error) == line


def test_a_short_new_password_is_refused_before_typing_it_again(monkeypatch):
    prompts = []
    monkeypatch.setattr(db.getpass, "getpass", lambda prompt: prompts.append(prompt) or "short")
    with pytest.raises(db.DatabaseError, match="at least"):
        db.ask_new_password()
    assert len(prompts) == 1


def test_a_new_row_in_exported_isnt_finished_yet(conn):
    with conn:
        conn.execute("INSERT INTO exported (job_id, folder, exported_on) VALUES (7, 'a', 'now')")
    assert conn.execute("SELECT finished_on FROM exported").fetchone() == (None,)


# The jobs rebuild for feeds. Found by its table's name, so it stays this rebuild's test when other upgrades are added.
REBUILD = next(version for version, step in enumerate(db.UPGRADES, 1) if "jobs_v21" in step)
OLD_REASONS = (
    "removed from its board", "board shut down", "company stopped hiring", "removed from Simplify",
    "inactive on Simplify", "reason not recorded (before 2026-10-21)",
)  # fmt: skip
NEW_REASONS = ("no longer in the feed", "feed turned off")
ODD_TEXT = 'Café d\'été "Ñandú" 日本語 -- ; DROP TABLE jobs;\n' * 2000
FIRST_JOB = 1
SCORED = ("stage_a", "ai_scores", "ai_failures")


def _use_steps(patch, steps):
    patch.setattr(db, "UPGRADES", steps)
    patch.setattr(db, "SCHEMA_VERSION", len(steps))


def _table_names(conn):
    return [name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()]


def _children(conn):
    """Every table with a foreign key to jobs, as the database itself says."""
    keys = {table: conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall() for table in _table_names(conn)}
    return {table: rows for table, rows in keys.items() if any(row[2] == "jobs" for row in rows)}


def _place_row(conn, table, job_id):
    """A row in any table: its required columns, made up (the filling connection ignores checks)."""
    columns = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    needed = {name: 1 if kind == "INTEGER" else "x" for _, name, kind, required, default, _ in columns
              if required and default is None}  # fmt: skip
    row = needed | {"job_id": job_id}
    sql = f'INSERT INTO "{table}" ({", ".join(row)}) VALUES ({", ".join("?" * len(row))})'  # noqa: S608 (test helper)
    conn.execute(sql, list(row.values()))


def _every_row(conn):
    """Every row of every table, compared whole so that no table is left out by hand."""
    return {table: conn.execute(f'SELECT * FROM "{table}"').fetchall() for table in _table_names(conn)}  # noqa: S608


def _schema(conn):
    return conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").fetchall()


def _version(conn):
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _read(path, *readers):
    with _open_as_is(path / db.DB_FILE) as conn:
        return [reader(conn) for reader in readers]


@pytest.fixture
def old_jobs(tmp_path, monkeypatch):
    """A data folder really made by the upgrades before the rebuild, with a job of each kind and a row everywhere."""
    path = tmp_path / "data"
    with monkeypatch.context() as earlier:
        _use_steps(earlier, db.UPGRADES[: REBUILD - 1])
        db.setup(path, PASSWORD)
        with closing(db.connect(path)) as conn, conn:
            conn.execute("PRAGMA ignore_check_constraints = ON")
            add_job(
                conn, "ML Engineer", sources=["lever:zeta", "greenhouse:acme"], description=ODD_TEXT, salary="$1",
                not_interested=1, location_by_you=1, added_by_you=1, check_location=1, open_manually_only=1,
                prepare_anyway=1, prepare_requested_on="2026-10-02", writer_sources="[1]",
            )  # fmt: skip
            for reason in OLD_REASONS:
                add_job(conn, reason, status="no longer available", status_reason=reason, sources=["lever:zeta"])
            for table in _children(conn):
                _place_row(conn, table, FIRST_JOB)  # in job_sources, after the later jobs' rows
    return path


def _rebuild(path, monkeypatch, step=None):
    """Open the database with the rebuild (or a copy of it) as the newest upgrade."""
    with monkeypatch.context() as now:
        _use_steps(now, (*db.UPGRADES[: REBUILD - 1], step or db.UPGRADES[REBUILD - 1]))
        with closing(db.connect(path)):
            pass


def _no_comments_or_spaces(sql):
    return "".join(re.sub(r"--.*", "", sql).split())


def test_the_rebuild_keeps_every_row_of_every_table(old_jobs, monkeypatch):
    [before] = _read(old_jobs, _every_row)
    assert len(before["jobs"]) == 1 + len(OLD_REASONS)
    _rebuild(old_jobs, monkeypatch)
    assert _read(old_jobs, _version, _every_row) == [REBUILD, before]


def test_the_rebuild_changes_nothing_in_the_schema_but_the_two_reasons(old_jobs, monkeypatch):
    def columns(conn):
        return conn.execute("PRAGMA table_info(jobs)").fetchall()

    schema, table = _read(old_jobs, _schema, columns)
    _rebuild(old_jobs, monkeypatch)
    new_schema, new_table = _read(old_jobs, _schema, columns)
    assert new_table == table

    def others(rows):
        return [row for row in rows if row[:2] != ("table", "jobs")]

    assert others(new_schema) == others(schema)  # every other table, the indexes and the trigger, word for word
    assert {row[1] for row in others(schema)} >= {"jobs_match_key", "jobs_list", "job_text_changed"}
    old_sql = next(row[3] for row in schema if row[:2] == ("table", "jobs"))
    new_sql = next(row[3] for row in new_schema if row[:2] == ("table", "jobs"))
    added = "".join(f",'{reason}'" for reason in NEW_REASONS).replace(" ", "")
    assert _no_comments_or_spaces(new_sql).replace(added, "") == _no_comments_or_spaces(old_sql)  # every other check


def test_tables_that_point_at_jobs_still_do_and_every_row_has_its_job(old_jobs, monkeypatch):
    [before] = _read(old_jobs, _children)
    _rebuild(old_jobs, monkeypatch)
    children, rows, broken = _read(
        old_jobs, _children, _every_row, lambda conn: conn.execute("PRAGMA foreign_key_check").fetchall()
    )
    assert children == before
    assert {"tracker", "tracker_moves", "submissions", "forms", "form_answers", "preparations", "emails"} <= set(
        children
    )
    assert all(rows[table] for table in children)
    assert broken == []


def test_job_sources_keep_their_order(old_jobs, monkeypatch):
    def order(conn):
        return conn.execute("SELECT rowid, source, job_id FROM job_sources ORDER BY rowid").fetchall()

    [before] = _read(old_jobs, order)
    assert [job_id for _, _, job_id in before] != sorted(job_id for _, _, job_id in before)  # not simply by job
    _rebuild(old_jobs, monkeypatch)
    assert _read(old_jobs, order) == [before]


def test_nothing_goes_back_to_scoring_until_a_jobs_text_really_changes(old_jobs, monkeypatch):
    _rebuild(old_jobs, monkeypatch)
    with _open_as_is(old_jobs / db.DB_FILE) as conn, conn:

        def changed():
            return [one for table in SCORED for (one,) in conn.execute(f"SELECT changed FROM {table}")]  # noqa: S608

        assert changed() == [0, 0, 0]
        conn.execute("UPDATE jobs SET last_seen = '2026-11-05' WHERE id = ?", (FIRST_JOB,))
        assert changed() == [0, 0, 0]
        conn.execute("UPDATE jobs SET description = 'New text.' WHERE id = ?", (FIRST_JOB,))
        assert changed() == [1, 1, 1]


@pytest.mark.parametrize("reason", NEW_REASONS)
def test_the_two_new_reasons_are_accepted(old_jobs, monkeypatch, reason):
    _rebuild(old_jobs, monkeypatch)
    with _open_as_is(old_jobs / db.DB_FILE) as conn, conn:
        conn.execute("UPDATE jobs SET status_reason = ? WHERE id = ?", (reason, FIRST_JOB))


@pytest.mark.parametrize(("column", "value"), [("status_reason", "made up"), ("status", "made up")])
def test_anything_else_is_still_refused(old_jobs, monkeypatch, column, value):
    _rebuild(old_jobs, monkeypatch)
    with _open_as_is(old_jobs / db.DB_FILE) as conn, pytest.raises(sqlcipher3.IntegrityError):
        conn.execute(f"UPDATE jobs SET {column} = ? WHERE id = ?", (value, FIRST_JOB))  # noqa: S608


def test_the_backup_comes_first_and_holds_the_old_table(old_jobs, monkeypatch):
    backup, upgrade, there = old_jobs / f"{db.DB_FILE}.bak-v{REBUILD - 1}", db._upgrade, []
    before = _read(old_jobs, _schema, _every_row)
    monkeypatch.setattr(db, "_upgrade", lambda *values: (there.append(backup.exists()), upgrade(*values)))
    _rebuild(old_jobs, monkeypatch)
    assert there == [True]
    with _open_as_is(backup) as conn:
        assert [_schema(conn), _every_row(conn)] == before


def test_a_fault_after_the_old_table_is_dropped_leaves_it_whole_and_a_retry_works(old_jobs, monkeypatch):
    before = _read(old_jobs, _version, _schema, _every_row)
    rename = "ALTER TABLE jobs_v21 RENAME TO jobs;"
    assert db.UPGRADES[REBUILD - 1].index("DROP TABLE jobs;") < db.UPGRADES[REBUILD - 1].index(rename)
    broken = db.UPGRADES[REBUILD - 1].replace(rename, f"DROP INDEX no_such_index;\n{rename}")
    with pytest.raises(sqlcipher3.OperationalError, match="no_such_index"):
        _rebuild(old_jobs, monkeypatch, broken)
    assert _read(old_jobs, _version, _schema, _every_row) == before  # no jobs_v21; the old check, indexes and trigger
    _rebuild(old_jobs, monkeypatch)
    assert _read(old_jobs, _version) == [REBUILD]


def test_a_row_the_new_table_refuses_fails_the_whole_step(old_jobs, monkeypatch):
    with _open_as_is(old_jobs / db.DB_FILE) as conn, conn:
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute("UPDATE jobs SET status_reason = 'made up' WHERE id = ?", (FIRST_JOB,))
    before = _read(old_jobs, _version, _schema, _every_row)
    with pytest.raises(sqlcipher3.IntegrityError):
        _rebuild(old_jobs, monkeypatch)
    assert _read(old_jobs, _version, _schema, _every_row) == before


def test_a_phase_1_database_and_a_new_one_end_with_the_same_schema(v1_data_dir):
    with closing(db.connect(v1_data_dir)) as upgraded, closing(sqlcipher3.connect(":memory:")) as new:
        db._upgrade(new, 0)
        assert _version(upgraded) == _version(new) == db.SCHEMA_VERSION
        assert _schema(upgraded) == _schema(new)


def test_foreign_keys_are_off_while_the_rebuild_runs(old_jobs, monkeypatch):
    noted = "CREATE TABLE noted AS SELECT foreign_keys FROM pragma_foreign_keys;"
    _rebuild(old_jobs, monkeypatch, f"{db.UPGRADES[REBUILD - 1]};\n{noted}")
    assert _read(old_jobs, lambda conn: conn.execute("SELECT * FROM noted").fetchall()) == [[(0,)]]


def test_the_next_new_job_gets_a_fresh_id(old_jobs, monkeypatch):
    _rebuild(old_jobs, monkeypatch)
    with _open_as_is(old_jobs / db.DB_FILE) as conn, conn:
        highest = conn.execute("SELECT max(id) FROM jobs").fetchone()[0]
        assert add_job(conn, "ML Intern") == highest + 1
