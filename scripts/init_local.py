"""First-time local setup: writes .env (database password, settings) and the secrets/ folder. Never overwrites."""

import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
env = ROOT / ".env"
if env.exists():
    sys.exit(f"{env} already exists -- keeping it (the database was initialised with that password).")

(ROOT / "secrets").mkdir(exist_ok=True)
env.write_text(
    "\n".join([
        "# IntraForge Pay pilot -- local settings. Never commit this file.",
        f"IFPAY_DB_PASSWORD={secrets.token_urlsafe(24)}",
        "IFPAY_QR_TTL_SECONDS=120",
        "IFPAY_TIMEZONE=UTC",
        "IFPAY_COOKIE_SECURE=false",
        "",
    ]),
    encoding="utf-8",
)
print(f"wrote {env}")
