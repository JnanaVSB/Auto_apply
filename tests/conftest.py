"""Shared test setup: every test runs against a fake in-memory keychain, never the real OS keychain."""

import keyring
import pytest
from keyring.backend import KeyringBackend


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
def fake_keychain() -> MemoryKeychain:
    fake = MemoryKeychain()
    keyring.set_keyring(fake)
    return fake
