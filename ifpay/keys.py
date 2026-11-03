"""The HMAC key that signs QR payment codes. Created once, kept with the ledger in Pay's IntraForge schema (so it is in
IntraForge's backups and survives a new container), never sent anywhere."""

from __future__ import annotations

import secrets
import threading

from . import db

_lock = threading.Lock()
_cached: bytes | None = None


def signing_key() -> bytes:
    global _cached
    if _cached is None:
        with _lock, db.connection() as conn:
            conn.execute("INSERT INTO signing_keys (id, key) VALUES (1, %s) ON CONFLICT (id) DO NOTHING",
                         (secrets.token_bytes(32),))
            _cached = bytes(conn.execute("SELECT key FROM signing_keys WHERE id = 1").fetchone()["key"])
    return _cached


def forget() -> None:
    """Drop the in-process copy (a new database, in tests)."""
    global _cached
    _cached = None
