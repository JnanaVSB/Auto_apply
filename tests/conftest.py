"""Shared test setup: a fake in-memory keychain (never the real OS keychain), quick password hashing, and the
dashboard's throwaway database, clock and test client."""

from contextlib import closing

import keyring
import pytest
from fastapi.testclient import TestClient
from keyring.backend import KeyringBackend
from web_helpers import ADDRESS, PASSWORD, Clock

from app import db, fetch, keychain
from app.web import __main__ as web_command
from app.web import main, security


class MemoryKeychain(KeyringBackend):
    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password


@pytest.fixture(autouse=True)
def fake_keychain(monkeypatch) -> MemoryKeychain:
    monkeypatch.setattr(keychain, "SAFE_BACKENDS", (*keychain.SAFE_BACKENDS, MemoryKeychain.__module__))
    fake = MemoryKeychain()
    keyring.set_keyring(fake)
    return fake


@pytest.fixture(autouse=True)
def quick_hash(monkeypatch):
    monkeypatch.setattr(db, "RECOVERY_KDF_ITER", 1_000)
    monkeypatch.setattr(security, "SCRYPT", {"n": 2**10, "r": 8, "p": 1})  # the app always uses the full cost


@pytest.fixture
def no_waiting(monkeypatch):
    """Downloads never really wait (politeness gaps and retry pauses pass at once)."""

    class FakeTime:
        now = 0.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    monkeypatch.setattr(fetch, "time", FakeTime())


@pytest.fixture
def data_dir(tmp_path):
    """A throwaway database with a dashboard password (test modules may define their own)."""
    path = tmp_path / "data"
    db.setup(path, "fake-master-password-1")
    with closing(db.connect(path)) as conn, conn:
        conn.execute(web_command.SAVE_LOGIN, (security.hash_password(PASSWORD), "now"))
    return path


@pytest.fixture
def conn(data_dir):
    with closing(db.connect(data_dir)) as connection:
        yield connection


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def client(data_dir, clock):
    """The dashboard, not logged in, with a clock the test controls."""
    with TestClient(main.create_app(data_dir, security.Logins(clock=clock)), base_url=ADDRESS) as client:
        yield client
