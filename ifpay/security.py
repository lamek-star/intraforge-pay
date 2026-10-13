"""Passwords, sessions, CSRF and capabilities.

Authorization is capability-based (as in IntraForge, ADR-0008): code checks a capability string, never a role name.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque

CAPABILITIES: dict[str, frozenset[str]] = {
    "admin": frozenset({
        "admin.read", "users.manage", "allocations.request", "allocations.decide", "integration.manage",
        "audit.read", "receipts.read_all", "receipts.upload",
    }),
    # The checker in maker/checker; can also request, but never approve their own request.
    "supervisor": frozenset({"admin.read", "allocations.request", "allocations.decide", "audit.read", "receipts.read_all"}),
    "cashier": frozenset({"checkout.create", "shop.read", "receipts.read_merchant", "receipts.upload"}),
    "customer": frozenset({"wallet.read", "wallet.pay", "receipts.read_own"}),
}


def has_capability(user: dict | None, capability: str) -> bool:
    if not user or not user.get("active"):
        return False
    return capability in CAPABILITIES.get(user["role"], frozenset())


# --- passwords (scrypt, standard library) ------------------------------------------------------------------------------

_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_b64, digest_b64 = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    digest = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), dklen=32, **_SCRYPT)
    return hmac.compare_digest(digest, base64.b64decode(digest_b64))


# A fixed hash to compare against when the username does not exist, so a login for an unknown user takes as long as
# one for a known user.
DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(16))


def validate_new_password(password: str) -> str | None:
    if len(password) < 10:
        return "Password must be at least 10 characters."
    if len(password) > 200:
        return "Password is too long."
    return None


# --- sessions ----------------------------------------------------------------------------------------------------------

SESSION_COOKIE = "ifpay_session"
CSRF_HEADER = "x-csrf-token"


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def csrf_matches(expected: str, supplied: str | None) -> bool:
    return bool(supplied) and hmac.compare_digest(expected, supplied)


# --- login rate limiting -----------------------------------------------------------------------------------------------


class LoginRateLimiter:
    """At most `limit` failed attempts per key in `window` seconds (per process; a pilot runs one process)."""

    def __init__(self, limit: int = 10, window: float = 300.0):
        self.limit = limit
        self.window = window
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._failures[key]
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._prune(key, time.monotonic())) >= self.limit

    def record_failure(self, key: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._prune(key, now).append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
