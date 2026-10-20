"""Encrypted database: daily unlock from the OS keychain, recovery with a master password.

Files in the data folder:
- app.db: the SQLCipher-encrypted database, opened with a random 256-bit key.
- recovery.key: a tiny SQLCipher file locked with the master password; it holds only that key.
"""

import argparse
import getpass
import re
import secrets
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
SCHEMA_VERSION = 1
SQLITE_NOTADB = 26  # SQLCipher's "file is not a database": what a wrong key or password produces
KEY_PATTERN = re.compile(r"[0-9a-f]{64}")
SIMPLE_SEQUENCES = ("0123456789", "abcdefghijklmnopqrstuvwxyz", "qwertyuiop", "asdfghjkl", "zxcvbnm")


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


def _check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise DatabaseError(f"The master password must be at least {MIN_PASSWORD_LENGTH} characters.")
    text = password.lower()
    runs = (run * (len(text) // len(run) + 2) for seq in SIMPLE_SEQUENCES for run in (seq, seq[::-1]))
    if len(set(text)) == 1 or any(text in run for run in runs):
        raise DatabaseError(
            "The master password is too easy to guess (one repeated character or a simple sequence). "
            "Try a 4-5 word passphrase."
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


def _open_db(data_dir: Path, key_hex: str) -> sqlcipher3.Connection:
    conn = _connect(data_dir / DB_FILE, _raw_key(key_hex))
    if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:  # an emptied file opens with any key
        conn.close()
        raise DatabaseError("app.db is empty or damaged. Restore the data folder from a backup.")
    return conn


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
    _check_password(master_password)
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
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
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
    _check_password(new_password)
    conn, _ = _open_recovery(data_dir, old_password)
    with closing(conn):
        conn.execute(f"PRAGMA rekey = {_passphrase(new_password)}")


def new_recovery_key(data_dir: Path, master_password: str) -> None:
    _check_password(master_password)
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


def _ask_new_password() -> str:
    password = getpass.getpass(
        f"New master password ({MIN_PASSWORD_LENGTH}+ characters; a 4-5 word passphrase is easiest to remember): "
    )
    if password != getpass.getpass("Re-enter the password: "):
        raise DatabaseError("The two passwords do not match.")
    return password


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
            setup(data_dir, _ask_new_password(), replace_key=args.replace_key)
            print(
                "Database created. Store your master password somewhere safe (e.g. a password manager): "
                "if both it and the keychain entry are lost, the data cannot be recovered."
            )
        elif command == "recover":
            recover(data_dir, getpass.getpass("Master password: "))
            print("Database key restored to the keychain.")
        elif command == "new-recovery-key":
            connect(data_dir).close()  # check the keychain key before asking for a password
            new_recovery_key(data_dir, _ask_new_password())
            print("New recovery.key written. Store the new master password safely; any old one no longer works.")
        elif command == "change-password":
            old_password = getpass.getpass("Current master password: ")
            _open_recovery(data_dir, old_password)[0].close()  # check it before asking for the new one
            change_master_password(data_dir, old_password, _ask_new_password())
            print("Master password changed.")
    except (ConfigError, KeychainError, DatabaseError) as error:
        print(f"Error: {error}", file=sys.stderr)
    except sqlcipher3.DatabaseError as error:  # anything unexpected from the database library, without a traceback
        print(f"Error: the database could not be read or written ({error}).", file=sys.stderr)
    except OSError as error:  # only the file name, since full paths include the user's name
        where = f": {Path(error.filename).name}" if error.filename else ""
        print(f"Error: {error.strerror or error}{where}", file=sys.stderr)
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.", file=sys.stderr)
    else:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
