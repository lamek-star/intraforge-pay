"""The pilot demonstration, step by step, through the HTTP API (manual section 6)."""

from conftest import Session, balance_of, journal_count, merchant_payable

from ifpay import db

NOTICE = "SIMULATED VALUE — NO CASH VALUE"


def test_full_demo_flow(world):
    admin, cashier, customer = Session("admin"), Session("cashier"), Session("customer1")
    payable_before = merchant_payable()

    # Steps 1-2 (fixture): application configured and verified; 5 customers hold 100.00 via maker/checker.
    w = customer.get("/api/wallet").json()
    assert w["balance"] == "100.00" and w["notice"] == NOTICE

    # Step 3-4: the shopkeeper enters 25 and generates a QR.
    r = cashier.post("/api/shop/payment-requests", json={"amount": "25"})
    assert r.status_code == 200, r.text
    pr = r.json()
    assert pr["amount"] == "25.00" and pr["status"] == "pending" and pr["notice"] == NOTICE
    assert pr["qr_svg"].startswith("data:image/svg+xml;base64,") and pr["code"].startswith("IFPAY1.")

    # Steps 5-6: the customer scans it and sees the verified shop and the amount.
    seen = customer.post("/api/wallet/checkout/resolve", json={"code": pr["code"]}).json()
    assert seen["merchant"] == {"id": str(world["merchant"]["id"]), "name": "Pilot Shop", "verified": True}
    assert seen["amount"] == "25.00" and seen["amount_minor"] == 2500 and seen["status"] == "pending"

    # Step 7: explicit confirmation is required.
    r = customer.post("/api/wallet/checkout/pay", json={"code": pr["code"], "expected_amount_minor": 2500,
                                                        "confirm": False})
    assert r.status_code == 400 and r.json()["code"] == "not_confirmed"
    assert balance_of("customer1") == 10_000

    r = customer.post("/api/wallet/checkout/pay", json={"code": pr["code"], "expected_amount_minor": 2500,
                                                        "confirm": True})
    assert r.status_code == 200, r.text
    paid = r.json()
    assert paid["status"] == "paid"

    # Step 8-9: 100 -> 75; merchant simulated payable +25.
    assert paid["balance"] == "75.00" and balance_of("customer1") == 7_500
    assert merchant_payable() == payable_before + 2_500
    assert cashier.get(f"/api/shop/payment-requests/{pr['id']}").json()["status"] == "paid"

    # Step 10: both parties get the same receipt.
    number = paid["receipt"]["receipt_number"]
    from_customer = customer.get(f"/api/receipts/{number}").json()
    from_shop = cashier.get(f"/api/receipts/{number}").json()
    assert from_customer["sha256"] == from_shop["sha256"] == paid["receipt"]["sha256"]
    assert from_customer["body"] == from_shop["body"]
    body = from_shop["body"]
    assert body["notice"] == NOTICE and body["amount"] == "25.00" and body["merchant"]["name"] == "Pilot Shop"
    assert body["journal_entries"] == [
        {"account": "CUST-customer1", "side": "debit", "amount": "25.00"},
        {"account": f"MERCH-{str(world['merchant']['id'])[:8]}", "side": "credit", "amount": "25.00"},
    ]

    # Step 11: the receipt was stored in the authorized IntraForge bucket (automatically, after the commit).
    assert len(from_shop["uploads"]) == 1 and from_shop["uploads"][0]["filename"] == f"{number}.txt"
    stored = world["intraforge"].files[from_shop["uploads"][0]["intraforge_file_id"]]
    assert stored["bucket"] == world["intraforge"].bucket_id
    # ...and an explicit upload is idempotent (no second file).
    r = cashier.post(f"/api/shop/receipts/{number}/upload")
    assert r.status_code == 200 and r.json()["intraforge_file_id"] == from_shop["uploads"][0]["intraforge_file_id"]
    assert len(world["intraforge"].files) == 1

    # Shop view: today's transactions and total simulated sales.
    shop = cashier.get("/api/shop").json()
    assert shop["total_simulated_sales"] == "25.00" and shop["today_total"] == "25.00"
    assert shop["today"][0]["receipt_number"] == number and shop["today"][0]["uploaded"] is True
    assert shop["notice"] == NOTICE

    # Step 12: the administrator sees it in the audit records and the journal.
    actions = [(e["action"], e["subject_id"]) for e in admin.get("/api/admin/audit").json()]
    assert ("payment.confirm", paid["receipt"]["body"]["journal_transaction_id"]) in actions
    assert ("intraforge.receipt.upload", number) in actions
    tx = admin.get("/api/admin/transactions").json()[0]
    assert tx["kind"] == "purchase" and tx["receipt_number"] == number
    overview = admin.get("/api/admin/overview").json()
    assert overview["conservation"]["ok"] is True
    assert overview["conservation"]["issued"] == "500.00"
    assert journal_count("purchase") == 1

    # Customer history shows the purchase with its receipt.
    history = customer.get("/api/wallet").json()["history"]
    assert history[0]["amount"] == "-25.00" and history[0]["receipt_number"] == number


def test_every_screen_and_receipt_carries_the_simulated_notice(world):
    cashier, customer = Session("cashier"), Session("customer2")
    pr = cashier.post("/api/shop/payment-requests", json={"amount": "1.50"}).json()
    paid = customer.post("/api/wallet/checkout/pay", json={"code": pr["code"], "expected_amount_minor": 150,
                                                           "confirm": True}).json()
    from ifpay import receipts

    with db.connection() as conn:
        receipt = conn.execute("SELECT * FROM receipts").fetchone()
    text = receipts.render_text(receipt).decode()
    assert text.count(NOTICE) == 2 + 1  # banner, footer, and inside the canonical JSON
    for page in ("login", "admin", "shop", "wallet"):
        html = customer.get(f"/{page}").text
        assert NOTICE in html, page
    assert paid["notice"] == NOTICE
