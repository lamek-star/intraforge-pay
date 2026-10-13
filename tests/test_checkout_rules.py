"""Insufficient balance, duplicate confirmation, expired / cancelled / tampered QR codes, amount mismatch."""

import time

import psycopg
import pytest
from conftest import Session, balance_of, journal_count, merchant_payable

from ifpay import checkout, db


def _qr(cashier, amount):
    r = cashier.post("/api/shop/payment-requests", json={"amount": amount})
    assert r.status_code == 200, r.text
    return r.json()


def _pay(session, pr, expected=None):
    return session.post("/api/wallet/checkout/pay", json={
        "code": pr["code"], "expected_amount_minor": pr["amount_minor"] if expected is None else expected,
        "confirm": True})


def _audit(action):
    with db.connection() as conn:
        return conn.execute("SELECT * FROM audit_events WHERE action = %s ORDER BY id", (action,)).fetchall()


def test_insufficient_balance_is_refused_and_nothing_moves(world):
    cashier, customer = Session("cashier"), Session("customer1")
    pr = _qr(cashier, "150")
    r = _pay(customer, pr)
    assert r.status_code == 409 and r.json()["code"] == "insufficient_balance"
    assert balance_of("customer1") == 10_000 and merchant_payable() == 0 and journal_count("purchase") == 0
    assert cashier.get(f"/api/shop/payment-requests/{pr['id']}").json()["status"] == "pending"
    refused = _audit("payment.refused")
    assert refused and refused[-1]["result"] == "denied" and refused[-1]["detail"]["code"] == "insufficient_balance"

    # Spending exactly the whole balance is allowed; one hundredth more is not.
    assert _pay(customer, _qr(cashier, "100")).status_code == 200
    assert balance_of("customer1") == 0
    assert _pay(customer, _qr(cashier, "0.01")).json()["code"] == "insufficient_balance"


def test_duplicate_confirmation_never_charges_twice(world):
    cashier, customer = Session("cashier"), Session("customer1")
    pr = _qr(cashier, "25")
    first = _pay(customer, pr)
    second = _pay(customer, pr)
    assert first.status_code == 200 and first.json()["status"] == "paid"
    assert second.status_code == 200 and second.json()["status"] == "already_paid"
    assert second.json()["receipt"]["receipt_number"] == first.json()["receipt"]["receipt_number"]
    assert balance_of("customer1") == 7_500 and journal_count("purchase") == 1

    # Another customer cannot pay an already-paid code either.
    other = _pay(Session("customer2"), pr)
    assert other.status_code == 409 and other.json()["code"] == "already_paid"
    assert balance_of("customer2") == 10_000


def test_expired_qr_is_refused(world):
    cashier, customer = Session("cashier"), Session("customer1")
    with db.connection() as conn:
        row = checkout.create_payment_request(conn, actor=world["cashier"], amount_minor=1_000,
                                              merchant_verified=True, ttl_seconds=1)
    from ifpay import qr

    pr = {"code": qr.make_code(row), "amount_minor": 1_000, "id": str(row["id"])}
    time.sleep(1.3)
    seen = customer.post("/api/wallet/checkout/resolve", json={"code": pr["code"]}).json()
    assert seen["status"] == "expired"
    r = _pay(customer, pr)
    assert r.status_code == 410 and r.json()["code"] == "expired"
    assert balance_of("customer1") == 10_000 and journal_count("purchase") == 0
    assert cashier.get(f"/api/shop/payment-requests/{pr['id']}").json()["status"] == "expired"


def test_database_refuses_to_mark_an_expired_request_paid(world):
    with db.connection() as conn:
        row = checkout.create_payment_request(conn, actor=world["cashier"], amount_minor=100,
                                              merchant_verified=True, ttl_seconds=1)
    time.sleep(1.2)
    with pytest.raises(psycopg.errors.CheckViolation, match="expired"):
        with db.connection() as conn:
            conn.execute("UPDATE payment_requests SET status = 'paid', paid_by = %s, paid_at = now(), "
                         "journal_transaction_id = (SELECT id FROM journal_transactions LIMIT 1) WHERE id = %s",
                         (world["customers"][0]["id"], row["id"]))


def test_cancelled_qr_is_refused(world):
    cashier, customer = Session("cashier"), Session("customer1")
    pr = _qr(cashier, "5")
    assert cashier.post(f"/api/shop/payment-requests/{pr['id']}/cancel").json()["status"] == "cancelled"
    r = _pay(customer, pr)
    assert r.status_code == 409 and r.json()["code"] == "cancelled"
    assert balance_of("customer1") == 10_000


def test_amount_mismatch_is_refused(world):
    cashier, customer = Session("cashier"), Session("customer1")
    pr = _qr(cashier, "25")
    r = _pay(customer, pr, expected=250)
    assert r.status_code == 409 and r.json()["code"] == "amount_mismatch"
    assert balance_of("customer1") == 10_000


@pytest.mark.parametrize("mutate", [
    lambda c: c[:-1] + ("A" if c[-1] != "A" else "B"),          # signature altered
    lambda c: c.replace(c.split(".")[1], "0" * 32),             # another request id
    lambda c: "IFPAY1.not-a-code",
    lambda c: "",
])
def test_tampered_or_garbage_codes_are_refused(world, mutate):
    cashier, customer = Session("cashier"), Session("customer1")
    pr = _qr(cashier, "25")
    bad = mutate(pr["code"])
    r = customer.post("/api/wallet/checkout/resolve", json={"code": bad})
    assert r.status_code in (400, 404)
    r = customer.post("/api/wallet/checkout/pay", json={"code": bad, "expected_amount_minor": 2500, "confirm": True})
    assert r.status_code in (400, 404)
    assert balance_of("customer1") == 10_000


@pytest.mark.parametrize("amount", ["0", "-1", "1000.01", "12.345", "1e3", "", "  "])
def test_invalid_amounts_cannot_make_a_qr(world, amount):
    r = Session("cashier").post("/api/shop/payment-requests", json={"amount": amount})
    assert r.status_code == 400
