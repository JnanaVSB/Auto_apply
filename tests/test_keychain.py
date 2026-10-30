import keyring
import pytest
from keyring.errors import KeyringLocked

from app.keychain import SAFE_BACKENDS, SERVICE, KeychainError, delete_secret, get_secret, set_secret

FAKE_SECRET = "fake-secret-value-123"  # noqa: S105 (fake value, tests only)


def test_saved_secret_reads_back(fake_keychain):
    set_secret("db_key", FAKE_SECRET)
    assert get_secret("db_key") == FAKE_SECRET
    assert fake_keychain.store == {(SERVICE, "db_key"): FAKE_SECRET}


def test_missing_secret_reads_as_none():
    assert get_secret("db_key") is None


@pytest.mark.parametrize(
    "module", ["keyring.backends.fail", "keyring.backends.null", "keyrings.alt.file", "some.unknown.backend"]
)
@pytest.mark.parametrize("chained", [False, True])
def test_unsafe_keychain_is_refused(fake_keychain, module, chained):
    unsafe = type("UnsafeKeychain", (type(fake_keychain),), {"__module__": module})()
    if chained:
        fake_keychain.backends = [unsafe]
        unsafe = fake_keychain
    keyring.set_keyring(unsafe)
    with pytest.raises(KeychainError, match="No secure OS keychain"):
        set_secret("db_key", FAKE_SECRET)


@pytest.mark.parametrize("module", SAFE_BACKENDS)
def test_real_os_keychains_are_allowed(fake_keychain, module):
    keyring.set_keyring(type("OsKeychain", (type(fake_keychain),), {"__module__": module})())
    set_secret("db_key", FAKE_SECRET)
    assert get_secret("db_key") == FAKE_SECRET


def test_empty_combined_keychain_is_refused(fake_keychain):
    fake_keychain.backends = []
    with pytest.raises(KeychainError, match="No secure OS keychain"):
        set_secret("db_key", FAKE_SECRET)


def test_a_removed_secret_is_gone_and_removing_a_missing_one_is_fine(fake_keychain):
    set_secret("anthropic", FAKE_SECRET)
    delete_secret("anthropic")
    assert get_secret("anthropic") is None and fake_keychain.store == {}
    delete_secret("anthropic")  # not saved: nothing to do


ACTIONS = {
    "set_password": lambda: set_secret("db_key", FAKE_SECRET),
    "get_password": lambda: get_secret("db_key"),
    "delete_password": lambda: delete_secret("db_key"),
}


@pytest.mark.parametrize("method", ACTIONS)
def test_keychain_failure_never_shows_secret(fake_keychain, monkeypatch, method):
    def locked(*args):
        raise KeyringLocked(f"locked while handling {FAKE_SECRET}")

    monkeypatch.setattr(fake_keychain, method, locked)
    with pytest.raises(KeychainError) as error:
        ACTIONS[method]()
    assert FAKE_SECRET not in str(error.value)
    assert error.value.__suppress_context__  # the original error, which may hold the secret, is not shown
