"""The journal rules PostgreSQL enforces on its own, even against code that bypasses the application."""

import psycopg
import pytest
from conftest import Session, balance_of

from ifpay import db, ledger


def _raw(sql, params=()):
    """A write that skips the application entirely, as a buggy or hostile caller would."""
    with db.connection() as conn:
        conn.execute(sql, params)


def _accounts():
    with db.connection() as conn:
        rows = conn.execute("SELECT * FROM ledger_accounts").fetchall()
    by_kind = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(r)
    return by_kind


def _admin_id():
    with db.connection() as conn:
        return conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()["id"]


def test_unbalanced_transaction_is_refused(world):
    a = _accounts()
    with pytest.raises(psycopg.errors.CheckViolation, match="does not balance"):
        with db.connection() as conn:
            tx = conn.execute("INSERT INTO journal_transactions (kind, reference, created_by) VALUES "
                              "('allocation', 'BAD-1', %s) RETURNING id", (_admin_id(),)).fetchone()
            conn.execute("INSERT INTO journal_entries (transaction_id, account_id, side, amount_minor) VALUES "
                         "(%s, %s, 'debit', 500), (%s, %s, 'credit', 400)",
                         (tx["id"], a["issuance_reserve"][0]["id"], tx["id"], a["customer_liability"][0]["id"]))


def test_single_entry_and_empty_transactions_are_refused(world):
    a = _accounts()
    with pytest.raises(psycopg.errors.CheckViolation, match="at least two"):
        with db.connection() as conn:
            conn.execute("INSERT INTO journal_transactions (kind, reference, created_by) VALUES "
                         "('allocation', 'EMPTY', %s)", (_admin_id(),))
    with pytest.raises(psycopg.errors.CheckViolation, match="at least two"):
        with db.connection() as conn:
            tx = conn.execute("INSERT INTO journal_transactions (kind, reference, created_by) VALUES "
                              "('allocation', 'ONE', %s) RETURNING id", (_admin_id(),)).fetchone()
            conn.execute("INSERT INTO journal_entries (transaction_id, account_id, side, amount_minor) VALUES "
                         "(%s, %s, 'credit', 100)", (tx["id"], a["customer_liability"][0]["id"]))


def test_zero_or_negative_amounts_are_refused(world):
    a = _accounts()
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.connection() as conn:
            tx = conn.execute("INSERT INTO journal_transactions (kind, reference, created_by) VALUES "
                              "('allocation', 'NEG', %s) RETURNING id", (_admin_id(),)).fetchone()
            conn.execute("INSERT INTO journal_entries (transaction_id, account_id, side, amount_minor) VALUES "
                         "(%s, %s, 'debit', -5), (%s, %s, 'credit', -5)",
                         (tx["id"], a["issuance_reserve"][0]["id"], tx["id"], a["customer_liability"][0]["id"]))


def test_customer_balance_can_never_go_negative_even_bypassing_the_app(world):
    a = _accounts()
    customer_acct = next(x for x in a["customer_liability"] if x["code"] == "CUST-customer1")
    merchant_acct = a["merchant_payable"][0]
    with pytest.raises(psycopg.errors.CheckViolation, match="insufficient simulated balance"):
        with db.connection() as conn:
            ledger.post_transaction(conn, kind="purchase", reference="RAW-OVERDRAFT", memo="", created_by=_admin_id(),
                                    entries=[ledger.Entry(customer_acct["id"], "debit", 10_001),
                                             ledger.Entry(merchant_acct["id"], "credit", 10_001)])
    assert balance_of("customer1") == 10_000


@pytest.mark.parametrize("table", ["journal_transactions", "journal_entries", "audit_events"])
def test_financial_and_audit_records_are_immutable(world, table):
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="immutable"):
        _raw(f"UPDATE {table} SET id = id")
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="immutable"):
        _raw(f"DELETE FROM {table}")
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="immutable"):
        _raw(f"TRUNCATE {table} CASCADE")


def test_receipts_are_immutable(world):
    cashier, customer = Session("cashier"), Session("customer1")
    pr = cashier.post("/api/shop/payment-requests", json={"amount": "5"}).json()
    customer.post("/api/wallet/checkout/pay", json={"code": pr["code"], "expected_amount_minor": 500, "confirm": True})
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _raw("UPDATE receipts SET amount_minor = 1")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _raw("DELETE FROM receipts")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _raw("UPDATE payment_requests SET status = 'cancelled'")  # a paid request cannot change


def test_balances_are_only_the_sum_of_the_journal(world):
    # There is no stored balance column to update: the view is the only source.
    with db.connection() as conn:
        cols = {r["column_name"] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'ledger_accounts'")}
        assert not any("balance" in c for c in cols)
        kind = conn.execute("SELECT table_type FROM information_schema.tables WHERE table_name = 'account_balances'"
                            ).fetchone()["table_type"]
    assert kind == "VIEW"


def test_ledger_conservation_after_mixed_activity(world):
    cashier = Session("cashier")
    for i, amount in enumerate(["25", "10.50", "99.99", "0.01", "64.50"], start=1):
        pr = cashier.post("/api/shop/payment-requests", json={"amount": amount}).json()
        Session(f"customer{i}").post("/api/wallet/checkout/pay", json={
            "code": pr["code"], "expected_amount_minor": pr["amount_minor"], "confirm": True})
    with db.connection() as conn:
        report = ledger.conservation_report(conn)
    assert report["ok"] is True
    assert report["total_debits_minor"] == report["total_credits_minor"]
    assert report["issued_minor"] == 50_000
    spent = 2500 + 1050 + 9999 + 1 + 6450
    assert report["merchant_payable_minor"] == spent
    assert report["customer_liability_minor"] == 50_000 - spent
    assert report["unbalanced_transactions"] == 0 and report["negative_wallets"] == 0
