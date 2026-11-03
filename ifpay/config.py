"""Runtime settings, read from the environment (and .env next to the project, if present).

Pay runs inside IntraForge as a service application (see ifpay/platform.py). IntraForge's Docker Compose file gives each
Pay container its slot's service token and database password; the database schema and login role come from IntraForge
itself (``GET self``). Secrets never come from the browser.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SIMULATED_NOTICE = "SIMULATED VALUE — NO CASH VALUE"
VERSION = "0.3.0"


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _secret(name: str) -> str:
    """A secret from NAME, or from the file named by NAME_FILE (Docker secrets)."""
    value = os.environ.get(name, "")
    path = os.environ.get(name + "_FILE")
    if not value and path and Path(path).is_file():
        value = Path(path).read_text(encoding="utf-8").strip()
    return value


@dataclass(frozen=True)
class Settings:
    # IntraForge's internal address (e.g. http://backend:8000) and this deployment slot's service token.
    intraforge_url: str
    service_token: str
    # Where Pay is served on IntraForge's origin, e.g. /apps/pay-1 (no trailing slash; "" = the root, tests only).
    base_path: str
    # The tenant PostgreSQL IntraForge provisioned Pay's schema in; the role and schema come from IntraForge.
    db_host: str
    db_port: int
    db_name: str
    db_password: str
    # Development/tests only: a complete database URL used instead of the IntraForge-provisioned schema.
    database_url: str | None
    qr_ttl_seconds: int
    session_hours: int
    cookie_secure: bool
    intraforge_verify_max_age_seconds: int
    max_document_bytes: int
    # The HTTPS origins phones use to reach IntraForge (comma-separated, e.g. https://192.168.1.10:8443), and the
    # certificate authority that signs IntraForge's HTTPS certificate (the proxy's root CA): the shop connect QR.
    public_urls: tuple[str, ...]
    ca_cert_file: Path | None

    def cookie_path(self) -> str:
        return (self.base_path or "") + "/"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    _load_dotenv(PROJECT_ROOT / ".env")
    base_path = os.environ.get("IFPAY_BASE_PATH", "").rstrip("/")
    if base_path and not base_path.startswith("/"):
        raise RuntimeError("IFPAY_BASE_PATH must start with '/', e.g. /apps/pay-1")
    ca = os.environ.get("IFPAY_CA_CERT", "")
    return Settings(
        intraforge_url=os.environ.get("IFPAY_INTRAFORGE_URL", "").rstrip("/"),
        service_token=_secret("IFPAY_SERVICE_TOKEN"),
        base_path=base_path,
        db_host=os.environ.get("IFPAY_DB_HOST", "postgres-tenant"),
        db_port=int(os.environ.get("IFPAY_DB_PORT", "5432")),
        db_name=os.environ.get("IFPAY_DB_NAME", "intraforge"),
        db_password=_secret("IFPAY_DB_PASSWORD"),
        database_url=os.environ.get("IFPAY_DATABASE_URL") or None,
        qr_ttl_seconds=int(os.environ.get("IFPAY_QR_TTL_SECONDS", "120")),
        session_hours=int(os.environ.get("IFPAY_SESSION_HOURS", "8")),
        cookie_secure=os.environ.get("IFPAY_COOKIE_SECURE", "true").lower() == "true",
        intraforge_verify_max_age_seconds=int(os.environ.get("IFPAY_INTRAFORGE_VERIFY_MAX_AGE", "60")),
        max_document_bytes=int(os.environ.get("IFPAY_MAX_DOCUMENT_BYTES", str(5 * 1024 * 1024))),
        public_urls=tuple(u.strip().rstrip("/") for u in os.environ.get("IFPAY_PUBLIC_URLS", "").split(",")
                          if u.strip()),
        ca_cert_file=Path(ca) if ca else None,
    )
