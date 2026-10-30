"""Store and read the user's secrets in the OS keychain, never in files."""

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

SERVICE = "auto_apply"
# Only the real OS keychains; anything else (plain files, "null", unknown add-ons) is refused.
SAFE_BACKENDS = (
    "keyring.backends.macOS",
    "keyring.backends.Windows",
    "keyring.backends.SecretService",
    "keyring.backends.libsecret",
    "keyring.backends.kwallet",
)


class KeychainError(Exception):
    """The OS keychain is missing, unsafe or failed. Messages never include secret values."""


def _keychain() -> keyring.backend.KeyringBackend:
    backend = keyring.get_keyring()
    parts = getattr(backend, "backends", [backend])  # a "chainer" backend combines several keychains
    unsafe = [type(part).__module__ for part in parts if type(part).__module__ not in SAFE_BACKENDS]
    if unsafe or not parts:
        raise KeychainError(f"No secure OS keychain available (found: {', '.join(unsafe) or 'none'}).")
    return backend


def set_secret(name: str, value: str) -> None:
    backend = _keychain()
    try:
        backend.set_password(SERVICE, name, value)
    except KeyringError as error:
        raise KeychainError(f"Could not save '{name}' in the OS keychain ({type(error).__name__}).") from None


def get_secret(name: str) -> str | None:
    backend = _keychain()
    try:
        return backend.get_password(SERVICE, name)
    except KeyringError as error:
        raise KeychainError(f"Could not read '{name}' from the OS keychain ({type(error).__name__}).") from None


def delete_secret(name: str) -> None:
    """Remove a secret; one that isn't saved is fine."""
    backend = _keychain()
    try:
        backend.delete_password(SERVICE, name)
    except PasswordDeleteError:
        return
    except KeyringError as error:
        raise KeychainError(f"Could not remove '{name}' from the OS keychain ({type(error).__name__}).") from None
