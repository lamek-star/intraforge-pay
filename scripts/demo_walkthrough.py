"""The pilot demonstration (manual section 6), driven end to end over HTTP against a running pilot server and the real
IntraForge it is connected to. Prints the evidence for each of the 12 steps and exits non-zero if any check fails.

  python scripts/demo_walkthrough.py [--server http://127.0.0.1:8600]

Uses the accounts from secrets/test-accounts.json and the IntraForge administrator session from
intraforge-sandbox/.sandbox-admin.json (to read IntraForge's own audit log in step 12). Run it on a fresh pilot
database: it allocates credits (step 2) to customers that have none yet and pays 25.00 as customer1.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from intraforge_admin_session import IntraForgeAdmin  # noqa: E402

NOTICE = "SIMULATED VALUE — NO CASH VALUE"
failures = 0


def check(label: str, condition: bool, evidence: str = "") -> None:
    global failures
    mark = "PASS" if condition else "FAIL"
    failures += 0 if condition else 1
    print(f"   [{mark}] {label}" + (f" -- {evidence}" if evidence else ""))


def session(server: str, username: str, accounts: dict) -> httpx.Client:
    c = httpx.Client(base_url=server, timeout=30)
    r = c.post("/api/auth/login", json={"username": username, "password": accounts[username]["password"]})
    r.raise_for_status()
    c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    return c


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="http://127.0.0.1:8600")
    args = parser.parse_args()
    accounts = json.loads((ROOT / "secrets" / "test-accounts.json").read_text(encoding="utf-8"))
    sandbox = json.loads((ROOT / "intraforge-sandbox" / ".sandbox-admin.json").read_text(encoding="utf-8"))
    config = json.loads((ROOT / "config" / "intraforge.json").read_text(encoding="utf-8"))
    admin = session(args.server, "admin", accounts)
    supervisor = session(args.server, "supervisor", accounts)
    cashier = session(args.server, "cashier", accounts)
    customer = session(args.server, "customer1", accounts)

    print("1. Administrator connects the Pilot Shop application (non-secret configuration wizard)")
    r = admin.put("/api/admin/integration", json=config).json()
    check("configuration accepted and verified with IntraForge", r["status"]["ok"] is True,
          f"application {config['application_id']}, bucket '{config['bucket_name']}'")
    integ = admin.get("/api/admin/integration").json()
    check("credential stays on the server (only its presence is reported)", integ["credential_file_present"]
          and "pdc_sk_" not in json.dumps(integ))

    print("2. Administrator assigns simulated credits to the five test customers (maker/checker)")
    customers = admin.get("/api/admin/customers").json()
    check("five test customers exist", len(customers) == 5, ", ".join(c["username"] for c in customers))
    for c in customers:
        if c["balance"] != "0.00":
            continue
        req = admin.post("/api/admin/allocations", json={"customer_id": c["id"], "amount": "100",
                                                         "reason": "Pilot opening allocation"}).json()
        own = admin.post(f"/api/admin/allocations/{req['id']}/approve", json={})
        check(f"maker cannot approve own request for {c['username']}", own.status_code == 403,
              own.json().get("code", ""))
        ok = supervisor.post(f"/api/admin/allocations/{req['id']}/approve", json={"note": "checked"})
        check(f"checker (supervisor) approves 100.00 for {c['username']}", ok.status_code == 200)
    balances = {c["username"]: c["balance"] for c in admin.get("/api/admin/customers").json()}
    check("customer1 starts with 100.00", balances["customer1"] == "100.00", str(balances))

    print("3-4. Shopkeeper enters 25 and generates a QR code")
    payable_before = cashier.get("/api/shop").json()["total_simulated_sales"]
    pr = cashier.post("/api/shop/payment-requests", json={"amount": "25"}).json()
    check("QR generated for 25.00", pr["amount"] == "25.00" and pr["qr_svg"].startswith("data:image/svg+xml"),
          f"code {pr['code'][:20]}…, expires {pr['expires_at']}")

    print("5-6. Customer scans the QR and sees the verified shop and amount")
    seen = customer.post("/api/wallet/checkout/resolve", json={"code": pr["code"]}).json()
    check("shop shown as verified", seen["merchant"]["verified"] is True, seen["merchant"]["name"])
    check("amount shown is 25.00", seen["amount"] == "25.00")

    print("7. Customer explicitly confirms the payment")
    unconfirmed = customer.post("/api/wallet/checkout/pay", json={"code": pr["code"], "expected_amount_minor": 2500,
                                                                  "confirm": False})
    check("payment without confirmation is refused", unconfirmed.status_code == 400)
    paid = customer.post("/api/wallet/checkout/pay", json={"code": pr["code"], "expected_amount_minor": 2500,
                                                           "confirm": True}).json()
    check("payment confirmed", paid["status"] == "paid", paid["receipt"]["receipt_number"])

    print("8. Customer balance 100 -> 75")
    check("customer balance is 75.00", customer.get("/api/wallet").json()["balance"] == "75.00")

    print("9. Merchant simulated payable +25")
    after = cashier.get("/api/shop").json()["total_simulated_sales"]
    check("merchant payable increased by 25.00",
          round(float(after) - float(payable_before), 2) == 25.00, f"{payable_before} -> {after}")

    print("10. Both parties receive the same receipt")
    number = paid["receipt"]["receipt_number"]
    a = customer.get(f"/api/receipts/{number}").json()
    b = cashier.get(f"/api/receipts/{number}").json()
    check("identical receipt (same SHA-256) for customer and shop", a["sha256"] == b["sha256"], a["sha256"])
    check("receipt carries the simulated-value notice", a["body"]["notice"] == NOTICE)

    print("11. Receipt uploaded to the authorized IntraForge bucket")
    upload = cashier.post(f"/api/shop/receipts/{number}/upload").json()
    file_id = upload["intraforge_file_id"]
    check("receipt stored in 'Pilot Shop Receipts'", bool(file_id), f"{upload['filename']} -> file {file_id}")

    print("12. IntraForge administrator sees the transaction in the audit records")
    pilot_audit = admin.get("/api/admin/audit", params={"action": "payment.confirm"}).json()
    check("pilot audit log has the payment", any(e["detail"].get("receipt") == number for e in pilot_audit))
    if_admin = IntraForgeAdmin(sandbox["base_url"], ca_bundle=sandbox["ca_bundle"])
    if_admin.login(sandbox["email"], sandbox["password"])
    events = if_admin.results(if_admin.ok("GET", f"organizations/{sandbox['organization_id']}/audit/",
                                          params={"action": "storage.file.upload", "resource_id": file_id}))
    check("IntraForge's own audit log has the receipt upload by the Pilot Shop application",
          len(events) == 1 and events[0]["context"].get("display_filename") == f"{number}.txt",
          f"IntraForge audit event {events[0]['id'] if events else '-'}")
    overview = admin.get("/api/admin/overview").json()["conservation"]
    check("ledger conservation holds", overview["ok"] is True,
          f"issued {overview['issued']} = customers {overview['customer_liability']} + shop {overview['merchant_payable']}")

    print(f"\n{'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'} -- {NOTICE}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
