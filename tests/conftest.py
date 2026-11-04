"""Shared test setup: a fake in-memory keychain (never the real OS keychain), quick password hashing, a test
database built once and copied for each test, and the dashboard's clock, fake web and test client."""

import shutil
import time
from contextlib import closing
from pathlib import Path

import httpx
import keyring
import pytest
from fastapi.testclient import TestClient
from helpers import ADDRESS, MASTER_PASSWORD, PASSWORD, Clock, FakeWeb
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

from app import config, db, fetch, keychain
from app.fetch import Fetcher
from app.web import __main__ as web_command
from app.web import main, preparer, security


class MemoryKeychain(KeyringBackend):
    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if self.store.pop((service, username), None) is None:
            raise PasswordDeleteError("not saved")


class FakeTime:
    """Stands in for the time module in downloads: records waits instead of sleeping."""

    def __init__(self) -> None:
        self.now = 0.0
        self.waits: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds


def _fake_secrets(patch: pytest.MonkeyPatch) -> MemoryKeychain:
    """An empty in-memory keychain, and quick password hashing (the app always uses the full cost)."""
    patch.setattr(keychain, "SAFE_BACKENDS", (*keychain.SAFE_BACKENDS, MemoryKeychain.__module__))
    patch.setattr(db, "RECOVERY_KDF_ITER", 1_000)
    patch.setattr(security, "SCRYPT", {"n": 2**10, "r": 8, "p": 1})
    fake = MemoryKeychain()
    keyring.set_keyring(fake)
    return fake


@pytest.fixture(autouse=True)
def fake_keychain(monkeypatch) -> MemoryKeychain:
    return _fake_secrets(monkeypatch)


@pytest.fixture(autouse=True)
def no_real_settings(tmp_path, monkeypatch):
    """Tests never read your own data/config.yaml: a test that needs settings writes its own file."""
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "no-settings" / "config.yaml")


@pytest.fixture
def no_waiting(monkeypatch) -> FakeTime:
    """Downloads never really wait (politeness gaps and retry pauses pass at once); returns the clock."""
    fake = FakeTime()
    monkeypatch.setattr(fetch, "time", fake)
    return fake


@pytest.fixture(scope="session")
def built_database(tmp_path_factory) -> tuple[Path, dict]:
    """A database with a dashboard password, built once per test run (each build takes about 0.2 s).

    Returns its folder and its keychain entries; data_dir gives each test its own copy.
    """
    path = tmp_path_factory.mktemp("built") / "data"
    with pytest.MonkeyPatch.context() as patch:
        keys = _fake_secrets(patch)
        db.setup(path, MASTER_PASSWORD)
        with closing(db.connect(path)) as conn, conn:
            conn.execute(web_command.SAVE_LOGIN, (security.hash_password(PASSWORD), "now"))
    return path, keys.store


@pytest.fixture
def data_dir(tmp_path, built_database, fake_keychain):
    """A throwaway copy of the test database, with its key in this test's fake keychain."""
    built, keys = built_database
    fake_keychain.store.update(keys)
    return shutil.copytree(built, tmp_path / "data")


@pytest.fixture
def conn(data_dir):
    with closing(db.connect(data_dir)) as connection:
        yield connection


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def web():
    """The fake web the dashboard's downloads reach: every address "not found" unless a test file gives its own."""
    return FakeWeb({})


@pytest.fixture
def client(data_dir, clock, web, monkeypatch):
    """The dashboard, not logged in, with a clock and a fake web the test controls. Its council runner never runs in
    the background (at start-up or on a press), so it can't touch a test's data or outlive it; a test that needs a
    pass runs one itself."""
    monkeypatch.setattr(preparer.Preparer, "wake", lambda self: None)
    app = main.create_app(data_dir, security.Logins(clock=clock), lambda: Fetcher(httpx.MockTransport(web)))
    with TestClient(app, base_url=ADDRESS) as client:
        yield client


@pytest.fixture
def phoenix():
    """This computer's time zone set to Phoenix (always 7 hours behind UTC), then put back."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("TZ", "America/Phoenix")
        time.tzset()
        yield
    time.tzset()
