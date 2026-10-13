"""Test fixtures. Tests run against the pilot's real PostgreSQL (docker compose service `postgres`), never mocks:
each test gets its own database cloned from a migrated template, and the demo database `ifpay` is never touched."""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from ifpay import allocations, config, db, directory, intraforge  # noqa: E402
from ifpay.web import api_auth  # noqa: E402
from fake_intraforge import FakeIntraForge  # noqa: E402

PASSWORD = "Pilot-Test-Password-1"
TEMPLATE = "ifpay_test_template"


def _server_url(dbname: str) -> str:
    config._load_dotenv(ROOT / ".env")
    password = os.environ["IFPAY_DB_PASSWORD"]
    return f"postgresql://ifpay:{password}@127.0.0.1:55432/{dbname}"


def _admin_exec(sql: str) -> None:
    with psycopg.connect(_server_url("postgres"), autocommit=True) as conn:
        conn.execute(sql)


@pytest.fixture(scope="session")
def template_db():
    _admin_exec(f"DROP DATABASE IF EXISTS {TEMPLATE} WITH (FORCE)")
    _admin_exec(f"CREATE DATABASE {TEMPLATE}")
    db.migrate(_server_url(TEMPLATE))
    yield TEMPLATE
    _admin_exec(f"DROP DATABASE IF EXISTS {TEMPLATE} WITH (FORCE)")


@pytest.fixture
def database(template_db, tmp_path, monkeypatch):
    name = f"ifpay_test_{uuid.uuid4().hex[:12]}"
    _admin_exec(f"CREATE DATABASE {name} TEMPLATE {template_db}")
    url = _server_url(name)
    monkeypatch.setenv("IFPAY_DATABASE_URL", url)
    monkeypatch.setenv("IFPAY_SECRET_DIR", str(tmp_path / "secrets"))
    monkeypatch.delenv("IFPAY_INTRAFORGE_CREDENTIAL_FILE", raising=False)
    config.get_settings.cache_clear()
    db.close_pool()
    db.open_pool(url, max_size=20)
    api_auth.login_limiter._failures.clear()
    yield url
    db.close_pool()
    config.get_settings.cache_clear()
    _admin_exec(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture
def fake_if(database, monkeypatch):
    """A configured, verified IntraForge connection backed by FakeIntraForge."""
    fake = FakeIntraForge()
    token = fake.issue()
    secret_file = config.get_settings().intraforge_credential_file
    secret_file.parent.mkdir(parents=True, exist_ok=True)
    secret_file.write_text(token, encoding="utf-8")
    fake.token = token
    monkeypatch.setattr(intraforge, "DEFAULT_TRANSPORT", fake.transport())
    with db.connection() as conn:
        intraforge.save_config(conn, config=intraforge.IntraForgeConfig.validate(fake.config()), actor_id=None)
    return fake


@pytest.fixture
def world(fake_if):
    """The pilot: admin (maker), supervisor (checker), cashier of Pilot Shop, five customers with 100.00 each."""
    with db.connection() as conn:
        admin = directory.create_user(conn, actor=None, username="admin", display_name="IntraForge Administrator",
                                      role="admin", password=PASSWORD)
        supervisor = directory.create_user(conn, actor=None, username="supervisor", display_name="Pilot Supervisor",
                                           role="supervisor", password=PASSWORD)
        cashier = directory.create_user(conn, actor=None, username="cashier", display_name="Shop Cashier",
                                        role="cashier", password=PASSWORD)
        merchant = directory.create_merchant(conn, name="Pilot Shop")
        directory.add_staff(conn, merchant_id=merchant["id"], user_id=cashier["id"])
        conn.execute("UPDATE merchants SET intraforge_application_id = %s", (fake_if.application_id,))
        customers = [
            directory.create_user(conn, actor=None, username=f"customer{i}", display_name=f"Test Customer {i}",
                                  role="customer", password=PASSWORD)
            for i in range(1, 6)
        ]
    for customer in customers:
        with db.connection() as conn:
            req = allocations.request_allocation(conn, actor=admin, customer_id=customer["id"], amount_minor=10_000,
                                                 reason="Pilot opening allocation")
        with db.connection() as conn:
            allocations.decide_allocation(conn, actor=supervisor, allocation_id=req["id"], approve=True)
    return {"admin": admin, "supervisor": supervisor, "cashier": cashier, "merchant": merchant,
            "customers": customers, "intraforge": fake_if}


class Session:
    """A browser-like client signed in as one user (own cookie jar, CSRF header attached)."""

    def __init__(self, username: str | None = None, password: str = PASSWORD):
        from ifpay.web.app import app

        self.client = TestClient(app)
        self.csrf = None
        if username:
            r = self.client.post("/api/auth/login", json={"username": username, "password": password})
            assert r.status_code == 200, r.text
            self.csrf = r.json()["csrf_token"]

    def get(self, path, **kw):
        return self.client.get(path, **kw)

    def post(self, path, json=None, csrf=True, **kw):
        headers = kw.pop("headers", {})
        if csrf and self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        return self.client.post(path, json=json, headers=headers, **kw)

    def put(self, path, json=None, **kw):
        return self.client.put(path, json=json, headers={"X-CSRF-Token": self.csrf or ""}, **kw)


@pytest.fixture
def login(database):
    return Session


def balance_of(username: str) -> int:
    with db.connection() as conn:
        return conn.execute(
            """SELECT b.balance_minor FROM account_balances b JOIN users u ON u.id = b.customer_id
               WHERE u.username = %s""", (username,)).fetchone()["balance_minor"]


def merchant_payable() -> int:
    with db.connection() as conn:
        return conn.execute(
            "SELECT balance_minor FROM account_balances WHERE kind = 'merchant_payable'").fetchone()["balance_minor"]


def journal_count(kind: str | None = None) -> int:
    with db.connection() as conn:
        if kind:
            return conn.execute("SELECT count(*) AS n FROM journal_transactions WHERE kind = %s",
                                (kind,)).fetchone()["n"]
        return conn.execute("SELECT count(*) AS n FROM journal_transactions").fetchone()["n"]
