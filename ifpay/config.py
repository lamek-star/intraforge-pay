"""Runtime settings, read from the environment (and .env next to the project, if present).

Secrets never come from the browser: the database password, the QR signing key and the IntraForge application
credential are read from the environment or from files under IFPAY_SECRET_DIR on the shop's own server.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SIMULATED_NOTICE = "SIMULATED VALUE — NO CASH VALUE"


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


@dataclass(frozen=True)
class Settings:
    database_url: str
    secret_dir: Path
    qr_ttl_seconds: int
    session_hours: int
    cookie_secure: bool
    intraforge_verify_max_age_seconds: int
    max_document_bytes: int
    public_port: int

    @property
    def tls_dir(self) -> Path:
        return self.secret_dir / "tls"

    @property
    def signing_key_file(self) -> Path:
        return self.secret_dir / "signing_key"

    @property
    def intraforge_credential_file(self) -> Path:
        override = os.environ.get("IFPAY_INTRAFORGE_CREDENTIAL_FILE")
        return Path(override) if override else self.secret_dir / "intraforge_credential"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    _load_dotenv(PROJECT_ROOT / ".env")
    database_url = os.environ.get("IFPAY_DATABASE_URL")
    if not database_url:
        password = os.environ.get("IFPAY_DB_PASSWORD")
        if not password:
            raise RuntimeError("IFPAY_DATABASE_URL (or IFPAY_DB_PASSWORD) is not set; run scripts/init_local.py first.")
        database_url = f"postgresql://ifpay:{password}@127.0.0.1:55432/ifpay"
    return Settings(
        database_url=database_url,
        secret_dir=Path(os.environ.get("IFPAY_SECRET_DIR", PROJECT_ROOT / "secrets")),
        qr_ttl_seconds=int(os.environ.get("IFPAY_QR_TTL_SECONDS", "120")),
        session_hours=int(os.environ.get("IFPAY_SESSION_HOURS", "8")),
        cookie_secure=os.environ.get("IFPAY_COOKIE_SECURE", "false").lower() == "true",
        intraforge_verify_max_age_seconds=int(os.environ.get("IFPAY_INTRAFORGE_VERIFY_MAX_AGE", "60")),
        max_document_bytes=int(os.environ.get("IFPAY_MAX_DOCUMENT_BYTES", str(5 * 1024 * 1024))),
        # The HTTPS port phones connect to (scripts/run_shop_server.ps1 sets it); goes into the shop connect code.
        public_port=int(os.environ.get("IFPAY_PUBLIC_PORT", "8643")),
    )


def signing_key() -> bytes:
    """The HMAC key that signs QR payment codes. Created on first use, kept only on this server."""
    path = get_settings().signing_key_file
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(32), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return bytes.fromhex(path.read_text(encoding="utf-8").strip())
