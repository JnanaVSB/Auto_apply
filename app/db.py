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


class DatabaseError(Exception):
    """The database can't be set up or unlocked. Messages never include keys or passwords."""


def _raw_key(key_hex: str) -> str:
    # The key comes from the keychain; checking its shape stops anything else reaching the PRAGMA.
    if not re.fullmatch(r"[0-9a-f]{64}", key_hex):
        raise DatabaseError("The stored database key is malformed. Run: python -m app.db recover")
    return f"\"x'{key_hex}'\""


def _passphrase(password: str) -> str:
    return "'" + password.replace("'", "''") + "'"


def _check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise DatabaseError(f"The master password must be at least {MIN_PASSWORD_LENGTH} characters.")


def _connect(path: Path, key: str, kdf_iter: int | None = None, *, create: bool = False) -> sqlcipher3.Connection:
    if not create and not path.is_file():  # connecting would otherwise create an empty file
        raise DatabaseError(f"{path.name} not found in the data folder. Run: python -m app.db setup")
    conn = sqlcipher3.connect(path)
    conn.execute("PRAGMA cipher_log_level = NONE")  # a wrong key would otherwise print SQLCipher's internal log
    conn.execute(f"PRAGMA key = {key}")
    if kdf_iter:
        conn.execute(f"PRAGMA kdf_iter = {kdf_iter}")
    try:
        conn.execute("SELECT count(*) FROM sqlite_master")  # SQLCipher only checks the key on first read
    except sqlcipher3.DatabaseError:
        conn.close()
        raise
    return conn


def _open_recovery(data_dir: Path, master_password: str) -> sqlcipher3.Connection:
    try:
        return _connect(data_dir / RECOVERY_FILE, _passphrase(master_password), RECOVERY_KDF_ITER)
    except sqlcipher3.DatabaseError:
        raise DatabaseError("Wrong master password.") from None


def setup(data_dir: Path, master_password: str) -> None:
    _check_password(master_password)
    db_path, recovery_path = data_dir / DB_FILE, data_dir / RECOVERY_FILE
    if db_path.exists() or recovery_path.exists():
        raise DatabaseError("A database already exists in the data folder; setup never overwrites it.")
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    key_hex = secrets.token_hex(32)
    try:
        with closing(_connect(recovery_path, _passphrase(master_password), RECOVERY_KDF_ITER, create=True)) as conn:
            conn.execute("CREATE TABLE recovery (db_key TEXT NOT NULL)")
            conn.execute("INSERT INTO recovery VALUES (?)", (key_hex,))
            conn.commit()
        set_secret(KEY_NAME, key_hex)
        with closing(_connect(db_path, _raw_key(key_hex), create=True)) as conn:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    except BaseException:
        db_path.unlink(missing_ok=True)  # no data yet, so removing a half-finished setup is safe
        recovery_path.unlink(missing_ok=True)
        raise
    db_path.chmod(0o600)
    recovery_path.chmod(0o600)


def connect(data_dir: Path) -> sqlcipher3.Connection:
    key_hex = get_secret(KEY_NAME)
    if key_hex is None:
        raise DatabaseError("Database key not found in the keychain. Run: python -m app.db recover")
    try:
        return _connect(data_dir / DB_FILE, _raw_key(key_hex))
    except sqlcipher3.DatabaseError:
        raise DatabaseError("The keychain's database key does not open app.db. Run: python -m app.db recover") from None


def recover(data_dir: Path, master_password: str) -> None:
    with closing(_open_recovery(data_dir, master_password)) as conn:
        key_hex = conn.execute("SELECT db_key FROM recovery").fetchone()[0]
    try:
        _connect(data_dir / DB_FILE, _raw_key(key_hex)).close()
    except sqlcipher3.DatabaseError:
        raise DatabaseError("recovery.key does not match app.db; the keychain was not changed.") from None
    set_secret(KEY_NAME, key_hex)


def change_master_password(data_dir: Path, old_password: str, new_password: str) -> None:
    _check_password(new_password)
    with closing(_open_recovery(data_dir, old_password)) as conn:
        conn.execute(f"PRAGMA rekey = {_passphrase(new_password)}")


def _ask_new_password() -> str:
    password = getpass.getpass(f"New master password ({MIN_PASSWORD_LENGTH}+ characters): ")
    if password != getpass.getpass("Repeat it: "):
        raise DatabaseError("The two passwords do not match.")
    return password


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.db", description="Set up or recover the encrypted database.")
    parser.add_argument("command", choices=["setup", "recover", "change-password"])
    command = parser.parse_args(argv).command
    try:
        data_dir = load_config().data_dir
        if command == "setup":
            setup(data_dir, _ask_new_password())
            print(
                "Database created. Store your master password somewhere safe (e.g. a password manager): "
                "if both it and the keychain entry are lost, the data cannot be recovered."
            )
        elif command == "recover":
            recover(data_dir, getpass.getpass("Master password: "))
            print("Database key restored to the keychain.")
        else:
            change_master_password(data_dir, getpass.getpass("Current master password: "), _ask_new_password())
            print("Master password changed.")
    except (ConfigError, KeychainError, DatabaseError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
