"""Dashboard login: the password hash, sessions with lock-out and logout after inactivity, anti-forgery tokens."""

import hashlib
import hmac
import math
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field

SCRYPT = {"n": 2**15, "r": 8, "p": 1}  # about 32 MB and 0.1 s per check, so guessing is slow
SCRYPT_MAXMEM = 64 * 2**20
IDLE_SECONDS = 30 * 60
MAX_SESSION_SECONDS = 12 * 60 * 60
MAX_WRONG = 5  # wrong passwords in a row before login is locked
LOCKOUT_SECONDS = 15 * 60
COOKIE = "session"
SAFETY_HEADERS = {
    # only the dashboard's own files; no inline scripts, nothing from the internet, never inside another site's page
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # job sites are not told where you came from; "no-referrer" would also hide where a form came from, which the
    # anti-forgery check needs
    "Referrer-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
}


def hash_password(password: str) -> str:
    """scrypt with a random salt; the settings are stored too, so they can be raised later."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, maxmem=SCRYPT_MAXMEM, **SCRYPT)
    return f"scrypt${SCRYPT['n']}${SCRYPT['r']}${SCRYPT['p']}${salt.hex()}${digest.hex()}"


def password_matches(password: str, stored: str) -> bool:
    _, n, r, p, salt, digest = stored.split("$")
    tried = hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), maxmem=SCRYPT_MAXMEM
    )
    return hmac.compare_digest(tried, bytes.fromhex(digest))


@dataclass
class Session:
    csrf: str  # the anti-forgery token every change must carry
    started: float
    last_seen: float


@dataclass
class Logins:
    """Who is logged in, and wrong passwords in a row. Kept in memory: stopping the dashboard logs everyone out."""

    clock: Callable[[], float] = time.monotonic
    sessions: dict[str, Session] = field(default_factory=dict)
    wrong: int = 0
    locked_until: float = 0.0

    def locked_minutes(self) -> int:
        """Minutes left of a lock-out; 0 when login is allowed."""
        return max(0, math.ceil((self.locked_until - self.clock()) / 60))

    def log_in(self, password: str, stored: str) -> str | None:
        """A new session's cookie value for the right password, None for a wrong one. Not for use while locked."""
        if not password_matches(password, stored):
            self.wrong += 1
            if self.wrong >= MAX_WRONG:
                self.wrong, self.locked_until = 0, self.clock() + LOCKOUT_SECONDS
            return None
        self.wrong, now = 0, self.clock()
        token = secrets.token_urlsafe(32)  # a fresh one at every login
        self.sessions[token] = Session(secrets.token_urlsafe(32), now, now)
        return token

    def session(self, token: str | None) -> Session | None:
        """The live session for this cookie, marked as used now; an ended one is forgotten."""
        session, now = self.sessions.get(token or ""), self.clock()
        if session and (now - session.last_seen > IDLE_SECONDS or now - session.started > MAX_SESSION_SECONDS):
            del self.sessions[token]
            return None
        if session:
            session.last_seen = now
        return session

    def log_out(self, token: str | None) -> None:
        self.sessions.pop(token or "", None)


def token_matches(sent: str | None, session: Session) -> bool:
    return hmac.compare_digest((sent or "").encode(), session.csrf.encode())
