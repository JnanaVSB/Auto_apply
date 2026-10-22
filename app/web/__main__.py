"""Open the dashboard: `python -m app.web [--port N]`; `python -m app.web reset-password` sets a new password."""

import argparse
import getpass
import socket
import sqlite3
import sys
import threading
import webbrowser
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import sqlcipher3
import uvicorn

from app import db
from app.collect import CollectError, _lock
from app.config import ConfigError, load_config
from app.keychain import KeychainError
from app.web import security
from app.web.main import create_app

HOST, DEFAULT_PORT = "127.0.0.1", 8765
DASHBOARD_LOCK = "dashboard-lock.db"
SAVE_LOGIN = """INSERT INTO dashboard_login (id, password_hash, set_on) VALUES (1, ?, ?)
    ON CONFLICT (id) DO UPDATE SET password_hash = excluded.password_hash, set_on = excluded.set_on"""


def _set_password(conn: sqlcipher3.Connection) -> None:
    password = getpass.getpass(
        "New dashboard password (12+ characters; a 4-5 word passphrase, different from your master password): "
    )
    db._check_password(password, "dashboard password")
    if password != getpass.getpass("Re-enter the password: "):
        raise db.DatabaseError("The two passwords do not match.")
    with conn:
        conn.execute(SAVE_LOGIN, (security.hash_password(password), datetime.now(UTC).isoformat(timespec="seconds")))


def _port_free(port: int) -> bool:
    with socket.socket() as probe:
        try:
            probe.bind((HOST, port))
        except OSError:
            return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.web", description="Open the jobs dashboard on this computer.")
    parser.add_argument("command", nargs="?", choices=["reset-password"])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"address number (default {DEFAULT_PORT})")
    args = parser.parse_args(argv)
    try:
        data_dir = load_config().data_dir
        busy = "The dashboard is already running in another window. Use that one, or close it first."
        with closing(_lock(data_dir, DASHBOARD_LOCK, busy)):
            with (  # the collection lock only while opening, so an upgrade never runs during a collection
                closing(_lock(data_dir, busy="A collection is running; start the dashboard when it finishes.")),
                closing(db.connect(data_dir)) as conn,
            ):
                if args.command or not conn.execute("SELECT 1 FROM dashboard_login").fetchone():
                    _set_password(conn)
                    print("Dashboard password saved.")
            if args.command:
                return 0
            if not _port_free(args.port):
                print(
                    f"Error: address number {args.port} is used by another program. "
                    f"Try: python -m app.web --port {args.port + 1}",
                    file=sys.stderr,
                )
                return 1
            url = f"http://{HOST}:{args.port}"
            print(f"Dashboard: {url}  (press Ctrl+C here to stop it)")
            threading.Timer(1.0, webbrowser.open, [url]).start()  # once the server is listening
            uvicorn.run(
                create_app(data_dir),
                host=HOST,
                port=args.port,
                access_log=False,  # addresses can hold search words; nothing about your jobs goes to the terminal
                log_level="warning",
                proxy_headers=False,
                server_header=False,
            )
    except (ConfigError, KeychainError, db.DatabaseError, CollectError) as error:
        print(f"Error: {error}", file=sys.stderr)
    except (sqlcipher3.Error, sqlite3.Error) as error:
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
