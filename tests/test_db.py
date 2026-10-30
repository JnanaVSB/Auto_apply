import functools
import os
import shutil
import sqlite3
import stat
import sys
from contextlib import closing

import pytest
import sqlcipher3
from helpers import MASTER_PASSWORD as PASSWORD

from app import db
from app.config import Config
from app.keychain import KeychainError, get_secret, set_secret

NEW_PASSWORD = "fake-master-password-2"  # noqa: S105 (fake value, tests only)


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
