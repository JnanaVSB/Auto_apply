import keyring
import pytest
from keyring.errors import KeyringLocked

from app.keychain import SERVICE, KeychainError, get_secret, set_secret

FAKE_SECRET = "fake-secret-value-123"  # noqa: S105 (fake value, tests only)


def test_saved_secret_reads_back(fake_keychain):
    set_secret("db_key", FAKE_SECRET)
    assert get_secret("db_key") == FAKE_SECRET
    assert fake_keychain.store == {(SERVICE, "db_key"): FAKE_SECRET}


def test_missing_secret_reads_as_none():
    assert get_secret("db_key") is None


@pytest.mark.parametrize("module", ["keyring.backends.fail", "keyring.backends.null", "keyrings.alt.file"])
@pytest.mark.parametrize("chained", [False, True])
def test_unsafe_keychain_is_refused(fake_keychain, module, chained):
    unsafe = type("UnsafeKeychain", (type(fake_keychain),), {"__module__": module})()
    if chained:
        fake_keychain.backends = [unsafe]
        unsafe = fake_keychain
    keyring.set_keyring(unsafe)
    with pytest.raises(KeychainError, match="No secure OS keychain"):
        set_secret("db_key", FAKE_SECRET)


@pytest.mark.parametrize("method", ["set_password", "get_password"])
def test_keychain_failure_never_shows_secret(fake_keychain, monkeypatch, method):
    def locked(*args):
        raise KeyringLocked(f"locked while handling {FAKE_SECRET}")

    monkeypatch.setattr(fake_keychain, method, locked)
    with pytest.raises(KeychainError) as error:
        set_secret("db_key", FAKE_SECRET) if method == "set_password" else get_secret("db_key")
    assert FAKE_SECRET not in str(error.value)
    assert error.value.__suppress_context__  # the original error, which may hold the secret, is not shown
