"""Start the pilot over with an empty database (synthetic pilot data only).

The journal, receipts and audit log are immutable by design, so there is no "undo": the only reset is to drop the whole
pilot database `ifpay` and let the server recreate it. Stop the pilot server first. IntraForge is not touched (receipts
already stored in its bucket stay there). Requires --yes-delete-all-pilot-data.
"""

import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ifpay.config import get_settings  # noqa: E402
from ifpay.db import migrate  # noqa: E402

if "--yes-delete-all-pilot-data" not in sys.argv:
    sys.exit(__doc__)

url = get_settings().database_url
if not url.rstrip("/").endswith("/ifpay"):
    sys.exit(f"Refusing: this script only resets the pilot database 'ifpay', not {url.rsplit('/', 1)[-1]!r}.")
admin_url = url.rsplit("/", 1)[0] + "/postgres"
with psycopg.connect(admin_url, autocommit=True) as conn:
    conn.execute("DROP DATABASE IF EXISTS ifpay WITH (FORCE)")
    conn.execute("CREATE DATABASE ifpay")
print("Applied:", migrate(url))
print("Pilot database recreated. Next: python scripts/seed_pilot.py, then load the IntraForge configuration again.")
