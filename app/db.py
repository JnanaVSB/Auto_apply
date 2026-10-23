"""Encrypted database: daily unlock from the OS keychain, recovery with a master password.

Files in the data folder:
- app.db: the SQLCipher-encrypted database, opened with a random 256-bit key.
- recovery.key: a tiny SQLCipher file locked with the master password; it holds only that key.
"""

import argparse
import getpass
import re
import secrets
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import sqlcipher3

from app.config import ConfigError, load_config
from app.keychain import KeychainError, get_secret, set_secret

DB_FILE = "app.db"
RECOVERY_FILE = "recovery.key"
KEY_NAME = "db_key"
MIN_PASSWORD_LENGTH = 12
RECOVERY_KDF_ITER = 1_024_000  # 4x SQLCipher's default, so every master-password guess is slow
SQLITE_NOTADB = 26  # SQLCipher's "file is not a database": what a wrong key or password produces
KEY_PATTERN = re.compile(r"[0-9a-f]{64}")
SIMPLE_SEQUENCES = ("0123456789", "abcdefghijklmnopqrstuvwxyz", "qwertyuiop", "asdfghjkl", "zxcvbnm")

# UPGRADES[n - 1] turns a version n - 1 database into version n. Append new steps; never edit old ones.
# "REFERENCES" is documentation only: SQLite leaves foreign keys unenforced unless asked, which the table
# rebuilds in steps 3 to 5 rely on.
UPGRADES = (
    "",  # version 1: the empty Phase 1 database
    """
    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY,
        apply_url TEXT NOT NULL UNIQUE,  -- tracking parameters removed
        match_key TEXT NOT NULL,  -- company + title + location, for the same job under another link
        company TEXT NOT NULL,
        title TEXT NOT NULL,
        locations TEXT NOT NULL,  -- JSON list
        us_location TEXT NOT NULL CHECK (us_location IN ('yes', 'unclear')),
        remote TEXT NOT NULL CHECK (remote IN ('remote', 'hybrid', 'on-site', 'unknown')),
        level TEXT NOT NULL CHECK (level IN ('intern', 'new grad', 'entry', 'mid', 'senior', 'staff+', 'unknown')),
        role_category TEXT,
        sponsorship TEXT NOT NULL CHECK (sponsorship IN ('offers', 'no', 'citizens only', 'unknown')),
        terms TEXT NOT NULL,  -- JSON list, e.g. ["Summer 2027"]
        description TEXT,  -- plain text; NULL until fetched
        salary TEXT,
        date_posted TEXT,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('open', 'closed')),
        open_manually_only INTEGER NOT NULL DEFAULT 0  -- the app must never open this link itself
    );
    CREATE INDEX jobs_match_key ON jobs (match_key);
    CREATE TABLE job_sources (
        source TEXT NOT NULL,  -- a Simplify list or a board, e.g. greenhouse:acme
        source_job_id TEXT NOT NULL,
        job_id INTEGER NOT NULL REFERENCES jobs (id),
        listed INTEGER NOT NULL,  -- 1 while the source still lists the job as open
        PRIMARY KEY (source, source_job_id)
    );
    CREATE INDEX job_sources_job ON job_sources (job_id);
    CREATE TABLE sources (
        name TEXT PRIMARY KEY,
        etag TEXT,  -- GitHub's version tag, so an unchanged list is not downloaded again
        last_ok TEXT
    );
    CREATE TABLE collect_runs (
        id INTEGER PRIMARY KEY,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        outcome TEXT,  -- done, cancelled or stopped
        new_jobs INTEGER NOT NULL DEFAULT 0,
        updated_jobs INTEGER NOT NULL DEFAULT 0,
        closed_jobs INTEGER NOT NULL DEFAULT 0,
        failures TEXT NOT NULL DEFAULT '[]'  -- JSON list of "source: reason"
    );
    """,
    """
    ALTER TABLE sources ADD COLUMN misses INTEGER NOT NULL DEFAULT 0;  -- runs in a row a board was not found
    ALTER TABLE sources ADD COLUMN retired_on TEXT;  -- the day a dead board stopped being read
    -- us_location may now also be 'no' (clearly not in the US). SQLite cannot change a CHECK rule in place,
    -- so the table is copied into one with the new rule, column for column.
    CREATE TABLE jobs_v3 (
        id INTEGER PRIMARY KEY,
        apply_url TEXT NOT NULL UNIQUE,
        match_key TEXT NOT NULL,
        company TEXT NOT NULL,
        title TEXT NOT NULL,
        locations TEXT NOT NULL,
        us_location TEXT NOT NULL CHECK (us_location IN ('yes', 'unclear', 'no')),
        remote TEXT NOT NULL CHECK (remote IN ('remote', 'hybrid', 'on-site', 'unknown')),
        level TEXT NOT NULL CHECK (level IN ('intern', 'new grad', 'entry', 'mid', 'senior', 'staff+', 'unknown')),
        role_category TEXT,
        sponsorship TEXT NOT NULL CHECK (sponsorship IN ('offers', 'no', 'citizens only', 'unknown')),
        terms TEXT NOT NULL,
        description TEXT,
        salary TEXT,
        date_posted TEXT,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('open', 'closed')),
        open_manually_only INTEGER NOT NULL DEFAULT 0
    );
    INSERT INTO jobs_v3 SELECT * FROM jobs;
    DROP TABLE jobs;
    ALTER TABLE jobs_v3 RENAME TO jobs;
    CREATE INDEX jobs_match_key ON jobs (match_key);
    """,
    """
    -- A run never hides or closes a job. Status 'no longer listed' replaces 'closed' ('closed' stays allowed for
    -- a later phase); two new marks. Jobs labelled 'no' become 'unclear' with the check mark, so none stays hidden.
    -- collect_runs.closed_jobs now counts jobs that became 'no longer listed'.
    CREATE TABLE jobs_v4 (
        id INTEGER PRIMARY KEY,
        apply_url TEXT NOT NULL UNIQUE,
        match_key TEXT NOT NULL,
        company TEXT NOT NULL,
        title TEXT NOT NULL,
        locations TEXT NOT NULL,
        us_location TEXT NOT NULL CHECK (us_location IN ('yes', 'unclear', 'no')),
        remote TEXT NOT NULL CHECK (remote IN ('remote', 'hybrid', 'on-site', 'unknown')),
        level TEXT NOT NULL CHECK (level IN ('intern', 'new grad', 'entry', 'mid', 'senior', 'staff+', 'unknown')),
        role_category TEXT,
        sponsorship TEXT NOT NULL CHECK (sponsorship IN ('offers', 'no', 'citizens only', 'unknown')),
        terms TEXT NOT NULL,
        description TEXT,
        salary TEXT,
        date_posted TEXT,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('open', 'no longer listed', 'closed')),
        open_manually_only INTEGER NOT NULL DEFAULT 0,
        added_by_you INTEGER NOT NULL DEFAULT 0,  -- picked with `add`: later runs never change its US label
        check_location INTEGER NOT NULL DEFAULT 0  -- a later run judged it outside the US: please check by hand
    );
    INSERT INTO jobs_v4 SELECT *, id IN (SELECT job_id FROM job_sources WHERE source = 'manual'), us_location = 'no'
        FROM jobs;
    UPDATE jobs_v4 SET status = 'no longer listed' WHERE status = 'closed';
    UPDATE jobs_v4 SET us_location = 'unclear' WHERE us_location = 'no';
    DROP TABLE jobs;
    ALTER TABLE jobs_v4 RENAME TO jobs;
    CREATE INDEX jobs_match_key ON jobs (match_key);
    """,
    """
    -- Every job that is not open says why, and each source entry says since when and why it stopped listing the
    -- job. 'no longer listed' becomes 'no longer available'. What the saved data can't tell is 'reason not recorded
    -- (before 2026-10-21)'; the next successful read of each source fills in the real reason.
    ALTER TABLE sources ADD COLUMN empty_runs INTEGER NOT NULL DEFAULT 0;  -- runs in a row a board with jobs sent none
    ALTER TABLE job_sources ADD COLUMN unlisted_on TEXT;  -- the day the app noticed this source stopped listing it
    ALTER TABLE job_sources ADD COLUMN unlisted_reason TEXT;  -- why, in the words of jobs.status_reason
    UPDATE job_sources SET unlisted_reason = CASE
        WHEN source IN (SELECT name FROM sources WHERE retired_on IS NOT NULL) THEN 'board shut down'
        ELSE 'reason not recorded (before 2026-10-21)' END
        WHERE listed = 0;
    CREATE TABLE jobs_v5 (
        id INTEGER PRIMARY KEY,
        apply_url TEXT NOT NULL UNIQUE,
        match_key TEXT NOT NULL,
        company TEXT NOT NULL,
        title TEXT NOT NULL,
        locations TEXT NOT NULL,
        us_location TEXT NOT NULL CHECK (us_location IN ('yes', 'unclear', 'no')),
        remote TEXT NOT NULL CHECK (remote IN ('remote', 'hybrid', 'on-site', 'unknown')),
        level TEXT NOT NULL CHECK (level IN ('intern', 'new grad', 'entry', 'mid', 'senior', 'staff+', 'unknown')),
        role_category TEXT,
        sponsorship TEXT NOT NULL CHECK (sponsorship IN ('offers', 'no', 'citizens only', 'unknown')),
        terms TEXT NOT NULL,
        description TEXT,
        salary TEXT,
        date_posted TEXT,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('open', 'no longer available', 'closed')),
        open_manually_only INTEGER NOT NULL DEFAULT 0,
        added_by_you INTEGER NOT NULL DEFAULT 0,
        check_location INTEGER NOT NULL DEFAULT 0,
        status_reason TEXT CHECK (status_reason IN ('removed from its board', 'board shut down',
            'company stopped hiring', 'removed from Simplify', 'inactive on Simplify',
            'reason not recorded (before 2026-10-21)'))  -- why it is not open; NULL while open
    );
    -- Renamed while copied, since the new table refuses the old name. A retired board gives the reason only when
    -- no board that is still read has the job.
    INSERT INTO jobs_v5 SELECT id, apply_url, match_key, company, title, locations, us_location, remote, level,
        role_category, sponsorship, terms, description, salary, date_posted, first_seen, last_seen,
        CASE status WHEN 'no longer listed' THEN 'no longer available' ELSE status END,
        open_manually_only, added_by_you, check_location,
        CASE WHEN status != 'no longer listed' THEN NULL
            WHEN EXISTS (SELECT 1 FROM job_sources WHERE job_id = jobs.id AND unlisted_reason = 'board shut down')
                AND NOT EXISTS (SELECT 1 FROM job_sources s LEFT JOIN sources r ON r.name = s.source
                    WHERE s.job_id = jobs.id AND s.source NOT LIKE 'simplify:%' AND r.retired_on IS NULL)
            THEN 'board shut down' ELSE 'reason not recorded (before 2026-10-21)' END
        FROM jobs;
    DROP TABLE jobs;
    ALTER TABLE jobs_v5 RENAME TO jobs;
    CREATE INDEX jobs_match_key ON jobs (match_key);
    """,
    """
    -- Phase 3 dashboard. "Not interested" only hides a job (never deletes it); a US label you set by hand is never
    -- changed by a run; the dashboard's own password is kept only as a slow hash, never as itself.
    ALTER TABLE jobs ADD COLUMN not_interested INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE jobs ADD COLUMN location_by_you INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE dashboard_login (
        id INTEGER PRIMARY KEY CHECK (id = 1),  -- one dashboard password per database
        password_hash TEXT NOT NULL,
        set_on TEXT NOT NULL
    );
    -- Everything the jobs list filters, sorts or shows, so listing never reads the long descriptions.
    CREATE INDEX jobs_list ON jobs (not_interested, status, status_reason, us_location, check_location, level, remote,
        added_by_you, role_category, sponsorship, terms, date_posted, first_seen, company, title, locations);
    """,
    """
    -- A board you stopped is never read again (and no longer decides whether its jobs are open) until you say so;
    -- nothing is deleted.
    ALTER TABLE sources ADD COLUMN stopped_on TEXT;  -- the day you stopped it; NULL while it is read
    """,
)
SCHEMA_VERSION = len(UPGRADES)


class DatabaseError(Exception):
    """The database can't be set up or unlocked. Messages never include keys or passwords."""


class _WrongKey(Exception):
    """The key or password does not open the file (unlike a locked or unreadable file)."""


def _raw_key(key_hex: str) -> str:
    # The key comes from the keychain; checking its shape stops anything else reaching the PRAGMA.
    if not KEY_PATTERN.fullmatch(key_hex):
        raise DatabaseError("The database key in the keychain is malformed. Run: python -m app.db recover")
    return f"\"x'{key_hex}'\""


def _passphrase(password: str) -> str:
    if "\0" in password:
        raise DatabaseError("The password contains an invalid (null) character.")
    return "'" + password.replace("'", "''") + "'"


def check_password(password: str, name: str = "master password") -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise DatabaseError(f"The {name} must be at least {MIN_PASSWORD_LENGTH} characters.")
    text = password.lower()
    runs = (run * (len(text) // len(run) + 2) for seq in SIMPLE_SEQUENCES for run in (seq, seq[::-1]))
    if len(set(text)) == 1 or any(text in run for run in runs):
        raise DatabaseError(
            f"The {name} is too easy to guess (one repeated character or a simple sequence). Try a 4-5 word passphrase."
        )


def _missing(data_dir: Path) -> DatabaseError:
    # Without app.db there is nothing to rescue, so every command points to the settings or a fresh setup.
    if not (data_dir / DB_FILE).is_file():
        return DatabaseError(
            "app.db not found in the data folder. "
            "Check data_dir in data/config.yaml; for a brand-new install run: python -m app.db setup"
        )
    return DatabaseError(
        "recovery.key not found in the data folder. "
        "If your database still opens, make a new one with: python -m app.db new-recovery-key"
    )


def _connect(path: Path, key: str, kdf_iter: int | None = None, *, create: bool = False) -> sqlcipher3.Connection:
    if not create and not path.is_file():  # connecting would otherwise create an empty file
        raise _missing(path.parent)
    conn = None
    try:
        conn = sqlcipher3.connect(path)
        conn.execute("PRAGMA cipher_log_level = NONE")  # a wrong key would otherwise print SQLCipher's internal log
        conn.execute(f"PRAGMA key = {key}")
        if kdf_iter:
            conn.execute(f"PRAGMA kdf_iter = {kdf_iter}")
        conn.execute("SELECT count(*) FROM sqlite_master")  # SQLCipher only checks the key on first read
        return conn
    except BaseException as error:
        if conn is not None:
            conn.close()
        if not isinstance(error, sqlcipher3.DatabaseError):
            raise
        if error.sqlite_errorcode == SQLITE_NOTADB:
            raise _WrongKey from None
        raise DatabaseError(f"{path.name} could not be read ({error}).") from None


def _open_recovery(data_dir: Path, master_password: str) -> tuple[sqlcipher3.Connection, str]:
    """Unlock recovery.key and return it, still open, with the database key it holds."""
    try:
        conn = _connect(data_dir / RECOVERY_FILE, _passphrase(master_password), RECOVERY_KDF_ITER)
    except _WrongKey:
        raise DatabaseError("Wrong master password.") from None
    try:
        row = conn.execute("SELECT db_key FROM recovery").fetchone()
    except sqlcipher3.DatabaseError:  # e.g. the table is missing, or the file is empty
        row = None
    key_hex = str(row[0]) if row else ""
    if not KEY_PATTERN.fullmatch(key_hex):
        conn.close()
        raise DatabaseError(
            "recovery.key is damaged; the keychain was not changed. "
            "If your database still opens, make a new one with: python -m app.db new-recovery-key"
        )
    return conn, key_hex


def _upgrade(conn: sqlcipher3.Connection, version: int) -> None:
    """Bring the database from `version` to SCHEMA_VERSION; each step is saved all-or-nothing."""
    for number in range(version + 1, SCHEMA_VERSION + 1):
        conn.executescript(f"BEGIN; {UPGRADES[number - 1]}; PRAGMA user_version = {number}; COMMIT;")


def _backup(conn: sqlcipher3.Connection, data_dir: Path, version: int) -> None:
    """Copy app.db, still encrypted, to app.db.bak-v<version>; swapped in only once complete.

    SQLite makes the copy itself (VACUUM INTO), so it is consistent even if another program has the database open.
    """
    backup = data_dir / f"{DB_FILE}.bak-v{version}"
    partial = data_dir / f"{backup.name}.partial"
    try:
        partial.unlink(missing_ok=True)  # VACUUM INTO refuses to overwrite a leftover from an interrupted copy
        conn.execute("VACUUM INTO ?", (str(partial),))
        partial.chmod(0o600)
        partial.replace(backup)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def _open_db(data_dir: Path, key_hex: str) -> sqlcipher3.Connection:
    conn = _connect(data_dir / DB_FILE, _raw_key(key_hex))
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise DatabaseError(
                "app.db was made by a newer version of auto_apply. Update this copy (git pull) and try again."
            )
        if version < 1:  # an emptied file opens with any key
            raise DatabaseError("app.db is empty or damaged. Restore the data folder from a backup.")
        if version < SCHEMA_VERSION:
            _backup(conn, data_dir, version)
            _upgrade(conn, version)
        _use_wal(conn)  # only after the checks: a refused database is left untouched
    except BaseException:
        conn.close()
        raise
    return conn


def _use_wal(conn: sqlcipher3.Connection) -> None:
    """WAL: readers never wait for a writer, so the dashboard keeps working while a collection saves.

    Kept in the file; while it is open SQLite adds app.db-wal and app.db-shm beside it (encrypted too).
    """
    conn.execute("PRAGMA journal_mode = WAL")


def _refuse_if_set_up(data_dir: Path, replace_key: bool) -> None:
    if (data_dir / DB_FILE).exists():
        raise DatabaseError(
            "A database already exists in the data folder; setup never overwrites it. "
            "To restore the keychain key run: python -m app.db recover. "
            "To change the master password run: python -m app.db change-password."
        )
    if not replace_key and get_secret(KEY_NAME) is not None:  # one key per user, so a new setup would replace it
        raise DatabaseError(
            "A database key from an earlier setup is already in the keychain; setup won't replace it. "
            "If your database is in another folder, check data_dir in data/config.yaml. "
            "To start fresh on purpose, run: python -m app.db setup --replace-key"
        )


def _write_recovery(path: Path, master_password: str, key_hex: str) -> None:
    with closing(_connect(path, _passphrase(master_password), RECOVERY_KDF_ITER, create=True)) as conn:
        conn.execute("CREATE TABLE recovery (db_key TEXT NOT NULL)")
        conn.execute("INSERT INTO recovery VALUES (?)", (key_hex,))
        conn.commit()
    path.chmod(0o600)


def setup(data_dir: Path, master_password: str, *, replace_key: bool = False) -> None:
    check_password(master_password)
    _refuse_if_set_up(data_dir, replace_key)
    db_path, recovery_path = data_dir / DB_FILE, data_dir / RECOVERY_FILE
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    data_dir.chmod(0o700)  # also when the folder already existed (e.g. created for config.yaml)
    if recovery_path.exists():  # left by an interrupted setup; set aside rather than deleted
        recovery_path.replace(data_dir / f"old-{RECOVERY_FILE}")
    key_hex = secrets.token_hex(32)
    try:
        _write_recovery(recovery_path, master_password, key_hex)
        with closing(_connect(db_path, _raw_key(key_hex), create=True)) as conn:
            _upgrade(conn, 0)
            _use_wal(conn)
        db_path.chmod(0o600)
        set_secret(KEY_NAME, key_hex)  # last, so a failed setup leaves nothing in the keychain
    except BaseException:
        db_path.unlink(missing_ok=True)  # no data yet, so removing a half-finished setup is safe
        recovery_path.unlink(missing_ok=True)
        raise


def connect(data_dir: Path) -> sqlcipher3.Connection:
    if not (data_dir / DB_FILE).is_file():  # checked before the keychain, so a fresh install isn't sent to recover
        raise _missing(data_dir)
    key_hex = get_secret(KEY_NAME)
    if key_hex is None:
        raise DatabaseError("Database key not found in the keychain. Run: python -m app.db recover")
    try:
        return _open_db(data_dir, key_hex)
    except _WrongKey:
        raise DatabaseError(
            "app.db could not be opened with the keychain key: the key is wrong or the file is damaged. "
            "If the key is wrong, run: python -m app.db recover. "
            "If the file is damaged, restore the data folder from a backup."
        ) from None


def recover(data_dir: Path, master_password: str) -> None:
    conn, key_hex = _open_recovery(data_dir, master_password)
    conn.close()
    try:
        _open_db(data_dir, key_hex).close()
    except _WrongKey:
        raise DatabaseError(
            "The key in recovery.key does not open app.db: they come from different setups, or app.db is damaged. "
            "The keychain was not changed."
        ) from None
    set_secret(KEY_NAME, key_hex)


def change_master_password(data_dir: Path, old_password: str, new_password: str) -> None:
    check_password(new_password)
    conn, _ = _open_recovery(data_dir, old_password)
    with closing(conn):
        conn.execute(f"PRAGMA rekey = {_passphrase(new_password)}")


def new_recovery_key(data_dir: Path, master_password: str) -> None:
    check_password(master_password)
    connect(data_dir).close()  # proves the keychain key opens app.db before it is copied
    key_hex = get_secret(KEY_NAME)
    new_path = data_dir / f"new-{RECOVERY_FILE}"
    new_path.unlink(missing_ok=True)  # leftover from an interrupted earlier run
    try:
        _write_recovery(new_path, master_password, key_hex)
        new_path.replace(data_dir / RECOVERY_FILE)  # swapped in only once complete
    except BaseException:
        new_path.unlink(missing_ok=True)
        raise


def ask_new_password(
    prompt: str = (
        f"New master password ({MIN_PASSWORD_LENGTH}+ characters; a 4-5 word passphrase is easiest to remember): "
    ),
    name: str = "master password",
) -> str:
    """Ask for a new password twice; a weak one is refused before it is typed again."""
    password = getpass.getpass(prompt)
    check_password(password, name)
    if password != getpass.getpass("Re-enter the password: "):
        raise DatabaseError("The two passwords do not match.")
    return password


def error_line(error: BaseException) -> str:
    """A terminal command's one-line message for an error: never a traceback, never a full path."""
    if isinstance(error, sqlcipher3.Error | sqlite3.Error):  # anything unexpected from the database library
        return f"Error: the database could not be read or written ({error})."
    if isinstance(error, OSError):  # only the file name, since full paths include the user's name
        where = f": {Path(error.filename).name}" if error.filename else ""
        return f"Error: {error.strerror or error}{where}"
    if isinstance(error, EOFError | KeyboardInterrupt):
        return "\nCancelled."
    return f"Error: {error}"  # our own errors, whose messages are written for the user


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.db", description="Set up or recover the encrypted database.")
    parser.add_argument("command", choices=["setup", "recover", "change-password", "new-recovery-key"])
    parser.add_argument("--replace-key", action="store_true", help="setup only: replace a key from an earlier setup")
    args = parser.parse_args(argv)
    command = args.command
    if args.replace_key and command != "setup":
        parser.error("--replace-key only works with setup")
    try:
        data_dir = load_config().data_dir
        if command == "setup":
            _refuse_if_set_up(data_dir, args.replace_key)  # before asking for a password that could not be used
            setup(data_dir, ask_new_password(), replace_key=args.replace_key)
            print(
                "Database created. Store your master password somewhere safe (e.g. a password manager): "
                "if both it and the keychain entry are lost, the data cannot be recovered."
            )
        elif command == "recover":
            recover(data_dir, getpass.getpass("Master password: "))
            print("Database key restored to the keychain.")
        elif command == "new-recovery-key":
            connect(data_dir).close()  # check the keychain key before asking for a password
            new_recovery_key(data_dir, ask_new_password())
            print("New recovery.key written. Store the new master password safely; any old one no longer works.")
        elif command == "change-password":
            old_password = getpass.getpass("Current master password: ")
            _open_recovery(data_dir, old_password)[0].close()  # check it before asking for the new one
            change_master_password(data_dir, old_password, ask_new_password())
            print("Master password changed.")
    except (
        ConfigError,
        KeychainError,
        DatabaseError,
        sqlcipher3.DatabaseError,
        OSError,
        EOFError,
        KeyboardInterrupt,
    ) as error:
        print(error_line(error), file=sys.stderr)
    else:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
