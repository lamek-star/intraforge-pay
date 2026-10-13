"""Create the pilot's test accounts and the shop (synthetic data only). Run once on the shop server.

  admin       IntraForge administrator for the pilot (maker of credit allocations)
  supervisor  Pilot supervisor (checker: approves allocations; cannot approve their own)
  cashier     Shop administrator/cashier of "Pilot Shop"
  customer1-5 Test customers (synthetic names)

Passwords are generated and written to <secret dir>/test-accounts.json (default secrets/; never printed).
With --allocate, also requests 100 IF Credits for each customer as `admin` and approves them as `supervisor` -- the
same maker/checker code path the Admin page uses. Without it, do the allocation in the Admin page (demo step 2).
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ifpay import allocations, db, directory  # noqa: E402
from ifpay.config import get_settings  # noqa: E402

CUSTOMERS = [
    ("customer1", "Test Customer One"),
    ("customer2", "Test Customer Two"),
    ("customer3", "Test Customer Three"),
    ("customer4", "Test Customer Four"),
    ("customer5", "Test Customer Five"),
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--allocate", action="store_true", help="also allocate 100 IF Credits per customer (maker/checker)")
    args = parser.parse_args()

    settings = get_settings()
    db.migrate(settings.database_url)
    db.open_pool(settings.database_url)
    out = settings.secret_dir / "test-accounts.json"
    accounts: dict = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}

    def ensure(conn, username, display_name, role):
        user = directory.get_user_by_username(conn, username)
        if user is None:
            password = secrets.token_urlsafe(9)
            user = directory.create_user(conn, actor=None, username=username, display_name=display_name, role=role,
                                         password=password)
            accounts[username] = {"password": password, "role": role, "display_name": display_name}
            print(f"created {role:10} {username}")
        return user

    with db.connection() as conn:
        admin = ensure(conn, "admin", "IntraForge Administrator", "admin")
        supervisor = ensure(conn, "supervisor", "Pilot Supervisor", "supervisor")
        cashier = ensure(conn, "cashier", "Shop Cashier", "cashier")
        merchant = directory.the_merchant(conn) or directory.create_merchant(conn, name="Pilot Shop")
        if not conn.execute("SELECT 1 FROM merchant_staff WHERE user_id = %s", (cashier["id"],)).fetchone():
            directory.add_staff(conn, merchant_id=merchant["id"], user_id=cashier["id"])
        customers = [ensure(conn, u, n, "customer") for u, n in CUSTOMERS]

    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(accounts, indent=2) + "\n", encoding="utf-8")
    print(f"test account passwords: {out}")

    if args.allocate:
        for customer in customers:
            with db.connection() as conn:
                already = conn.execute(
                    "SELECT 1 FROM credit_allocations WHERE customer_id = %s AND status <> 'rejected'", (customer["id"],)
                ).fetchone()
                if already:
                    continue
                request = allocations.request_allocation(conn, actor=admin, customer_id=customer["id"],
                                                         amount_minor=10_000, reason="Pilot opening allocation")
            with db.connection() as conn:
                allocations.decide_allocation(conn, actor=supervisor, allocation_id=request["id"], approve=True,
                                              note="Checked against the pilot plan")
            print(f"allocated 100.00 IF Credits to {customer['username']} (maker: admin, checker: supervisor)")
    db.close_pool()


if __name__ == "__main__":
    main()
