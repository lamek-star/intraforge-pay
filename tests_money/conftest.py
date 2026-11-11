"""Fixtures for the refund and cashback tests: a real PostgreSQL (never mocks), one fresh database per test, migrated
with every migration, and a small shop -- a merchant, a cashier, a maker, a supervisor and a funded customer.

    IFPAY_TEST_ADMIN_URL=postgresql://postgres:<password>@<host>:5432/postgres  python -m pytest tests_money
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ifpay import allocations, checkout, db, directory, keys, qr  # noqa: E402
from ifpay.security import staff_capabilities  # noqa: E402

ADMIN_URL = os.environ.get("IFPAY_TEST_ADMIN_URL", "")
PASSWORD = "Pilot-Test-Password-1"


def _admin(sql: str) -> None:
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(sql)


def _url(name: str) -> str:
    return ADMIN_URL.rsplit("/", 1)[0] + "/" + name


@pytest.fixture
def database():
    if not ADMIN_URL:
        pytest.skip("set IFPAY_TEST_ADMIN_URL to a PostgreSQL the tests may create databases in")
    name = f"ifpay_money_{uuid.uuid4().hex[:12]}"
    _admin(f"CREATE DATABASE {name}")
    url = _url(name)
    db.migrate(url)
    db.close_pool()
    keys.forget()
    db.open_pool(url, max_size=5)
    yield url
    db.close_pool()
    keys.forget()
    _admin(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


class Shop:
    def __init__(self, url: str):
        self.url = url

    def conn(self):
        return psycopg.connect(self.url, row_factory=dict_row)

    def run(self, fn, **kwargs):
        """One business operation in its own transaction, committed (so the deferred database checks run)."""
        with self.conn() as conn:
            result = fn(conn, **kwargs)
            conn.commit()
            return result

    def staff(self, name: str, *ifpay_caps: str) -> dict:
        with self.conn() as conn:
            row = directory.sync_staff(conn, intraforge_user_id=str(uuid.uuid4()), display_name=name,
                                       capabilities=staff_capabilities(set(ifpay_caps)), merchant=self.merchant)
            conn.commit()
            return row

    def fund(self, customer: dict, amount_minor: int) -> None:
        alloc = self.run(allocations.request_allocation, actor=self.maker, customer_id=customer["id"],
                         amount_minor=amount_minor, reason="test top-up")
        self.run(allocations.decide_allocation, actor=self.supervisor, allocation_id=alloc["id"], approve=True)

    def sale(self, customer: dict, amount_minor: int) -> dict:
        """A cashier's QR request paid by the customer; returns the receipt."""
        request = self.run(checkout.create_payment_request, actor=self.cashier, amount_minor=amount_minor,
                           merchant_verified=True, ttl_seconds=120)
        receipt, _ = self.run(checkout.pay, actor=customer, code=qr.make_code(request), expected_amount_minor=amount_minor)
        return receipt

    def balance(self, account_kind: str, owner_id) -> int:
        column = "customer_id" if account_kind == "customer" else "merchant_id"
        with self.conn() as conn:
            return int(conn.execute(f"SELECT balance_minor FROM account_balances WHERE {column} = %s",
                                    (owner_id,)).fetchone()["balance_minor"])

    def conservation(self) -> dict:
        from ifpay import ledger

        with self.conn() as conn:
            return ledger.conservation_report(conn)


@pytest.fixture
def shop(database) -> Shop:
    s = Shop(database)
    with s.conn() as conn:
        s.merchant = directory.create_merchant(conn, name="Test Shop")
        conn.commit()
    s.cashier = s.staff("Cashier", "ifpay.view", "ifpay.checkout")
    s.cashier2 = s.staff("Second cashier", "ifpay.view", "ifpay.checkout")
    s.maker = s.staff("Manager", "ifpay.view", "ifpay.customers.manage", "ifpay.allocations.request", "ifpay.checkout")
    s.supervisor = s.staff("Supervisor", "ifpay.view", "ifpay.allocations.approve", "ifpay.audit.read")
    with s.conn() as conn:
        s.customer = directory.create_user(conn, actor=None, username="customer1", display_name="Customer One",
                                           role="customer", password=PASSWORD)
        s.customer2 = directory.create_user(conn, actor=None, username="customer2", display_name="Customer Two",
                                            role="customer", password=PASSWORD)
        conn.commit()
    s.fund(s.customer, 100000)  # 1,000.00
    s.fund(s.customer2, 100000)
    return s
