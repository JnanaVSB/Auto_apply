"""Shared test setup: every test runs against a fake in-memory keychain, never the real OS keychain."""

import keyring
import pytest
from keyring.backend import KeyringBackend

from app import keychain


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
