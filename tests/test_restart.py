"""Transaction persistence across a restart of the pilot server process and of its PostgreSQL container.

The server runs as a real uvicorn process (not the in-process test client) against this test's own database.
"""

import os
import socket
import subprocess
import sys
import time

import httpx
import pytest
from conftest import PASSWORD, ROOT

from ifpay import checkout, db, ledger, qr

pytestmark = pytest.mark.restart
PG_CONTAINER = "ifpay-pilot-postgres-1"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, database_url: str, secret_dir: str):
        self.port = _free_port()
        self.env = {**os.environ, "IFPAY_DATABASE_URL": database_url, "IFPAY_SECRET_DIR": secret_dir}
        self.proc = None

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "ifpay.web.app:app", "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=ROOT, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                if httpx.get(self.url + "/healthz", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        raise RuntimeError("server did not start")

    def stop(self):
        self.proc.terminate()
        self.proc.wait(timeout=20)


def _client(server, username):
    c = httpx.Client(base_url=server.url)
    r = c.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert r.status_code == 200, r.text
    c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    return c


def _snapshot():
    with db.connection() as conn:
        return {
            "journal": [tuple(r.values()) for r in conn.execute(
                "SELECT t.id, t.reference, e.account_id, e.side, e.amount_minor FROM journal_transactions t "
                "JOIN journal_entries e ON e.transaction_id = t.id ORDER BY e.id")],
            "receipts": [(r["receipt_number"], r["body_sha256"]) for r in conn.execute(
                "SELECT receipt_number, body_sha256 FROM receipts ORDER BY receipt_number")],
            "conservation": ledger.conservation_report(conn),
        }


def test_transactions_survive_server_and_database_restarts(world, database, tmp_path):
    # The server process must share this test's QR signing key.
    from ifpay.config import get_settings, signing_key

    signing_key()
    server = Server(database, str(get_settings().secret_dir))
    server.start()
    try:
        with db.connection() as conn:
            row = checkout.create_payment_request(conn, actor=world["cashier"], amount_minor=2_500,
                                                  merchant_verified=True, ttl_seconds=600)
        code = qr.make_code(row)
        customer = _client(server, "customer1")
        paid = customer.post("/api/wallet/checkout/pay", json={"code": code, "expected_amount_minor": 2_500,
                                                               "confirm": True})
        assert paid.status_code == 200, paid.text
        number = paid.json()["receipt"]["receipt_number"]
        before = _snapshot()
        assert before["conservation"]["ok"]
    finally:
        server.stop()

    # 1. Server process restart.
    server.start()
    try:
        customer = _client(server, "customer1")
        assert customer.get("/api/wallet").json()["balance"] == "75.00"
        assert customer.get(f"/api/receipts/{number}").json()["sha256"] == before["receipts"][0][1]
        # The same QR code still cannot be charged again after the restart.
        again = customer.post("/api/wallet/checkout/pay", json={"code": code, "expected_amount_minor": 2_500,
                                                                "confirm": True})
        assert again.json()["status"] == "already_paid"
        assert _snapshot() == before

        # 2. PostgreSQL container restart while the server keeps running.
        subprocess.run(["docker", "restart", PG_CONTAINER], check=True, capture_output=True, timeout=120)
        deadline = time.time() + 60
        while True:
            try:
                r = customer.get("/api/wallet")
                if r.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.time() > deadline:
                raise AssertionError("server did not recover after the database restart")
            time.sleep(1)
        assert r.json()["balance"] == "75.00"
        assert customer.get(f"/api/receipts/{number}").status_code == 200
    finally:
        server.stop()
    db.close_pool()
    db.open_pool(database)
    assert _snapshot() == before
