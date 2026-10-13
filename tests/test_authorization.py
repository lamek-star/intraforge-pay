"""Unauthorized users: no session, wrong role, other people's receipts, missing CSRF, deactivated accounts."""

import pytest
from conftest import PASSWORD, Session, balance_of

from ifpay import db, directory

SHOP = [("GET", "/api/shop"), ("POST", "/api/shop/payment-requests")]
WALLET = [("GET", "/api/wallet"), ("POST", "/api/wallet/checkout/resolve"), ("POST", "/api/wallet/checkout/pay")]
ADMIN = [("GET", "/api/admin/overview"), ("GET", "/api/admin/audit"), ("GET", "/api/admin/transactions"),
         ("POST", "/api/admin/customers"), ("POST", "/api/admin/allocations"), ("PUT", "/api/admin/integration"),
         ("POST", "/api/admin/integration/verify")]


def _call(s: Session, method, path):
    body = {"amount": "1", "code": "x", "expected_amount_minor": 1, "confirm": True, "customer_id":
            "00000000-0000-0000-0000-000000000000", "username": "zz", "display_name": "z", "password": "x" * 12}
    if method == "GET":
        return s.get(path)
    if method == "PUT":
        return s.put(path, json=body)
    return s.post(path, json=body)


@pytest.mark.parametrize("method,path", SHOP + WALLET + ADMIN + [("GET", "/api/receipts/IFP-20260101-000001")])
def test_no_session_means_401(world, method, path):
    assert _call(Session(), method, path).status_code == 401


@pytest.mark.parametrize("who,forbidden", [
    ("customer1", SHOP + ADMIN),
    ("cashier", WALLET + ADMIN),
    ("supervisor", SHOP + WALLET + [("POST", "/api/admin/customers"), ("PUT", "/api/admin/integration")]),
    ("admin", SHOP + WALLET),
])
def test_roles_only_reach_their_own_functions(world, who, forbidden):
    s = Session(who)
    for method, path in forbidden:
        assert _call(s, method, path).status_code == 403, (who, method, path)
    assert balance_of("customer1") == 10_000


def test_denials_are_audited(world):
    Session("customer1").post("/api/shop/payment-requests", json={"amount": "5"})
    with db.connection() as conn:
        row = conn.execute("SELECT * FROM audit_events WHERE action = 'access.denied' ORDER BY id DESC").fetchone()
    assert row["actor_username"] == "customer1" and row["result"] == "denied"
    assert row["detail"]["capability"] == "checkout.create"


def test_customers_cannot_read_each_others_receipts(world):
    cashier, alice, bob = Session("cashier"), Session("customer1"), Session("customer2")
    pr = cashier.post("/api/shop/payment-requests", json={"amount": "5"}).json()
    number = alice.post("/api/wallet/checkout/pay", json={"code": pr["code"], "expected_amount_minor": 500,
                                                          "confirm": True}).json()["receipt"]["receipt_number"]
    assert alice.get(f"/api/receipts/{number}").status_code == 200
    # Same answer as a receipt that does not exist: receipt numbers cannot be probed.
    assert bob.get(f"/api/receipts/{number}").status_code == 404
    assert bob.get("/api/receipts/IFP-19990101-999999").status_code == 404
    assert Session("supervisor").get(f"/api/receipts/{number}").status_code == 200


def test_another_shops_cashier_sees_nothing_of_this_shop(world):
    with db.connection() as conn:
        other_shop = directory.create_merchant(conn, name="Other Shop")
        other = directory.create_user(conn, actor=None, username="othercashier", display_name="Other Cashier",
                                      role="cashier", password=PASSWORD)
        directory.add_staff(conn, merchant_id=other_shop["id"], user_id=other["id"])
    cashier, customer, intruder = Session("cashier"), Session("customer1"), Session("othercashier")
    pr = cashier.post("/api/shop/payment-requests", json={"amount": "5"}).json()
    number = customer.post("/api/wallet/checkout/pay", json={"code": pr["code"], "expected_amount_minor": 500,
                                                             "confirm": True}).json()["receipt"]["receipt_number"]
    assert intruder.get(f"/api/receipts/{number}").status_code == 404
    assert intruder.get(f"/api/shop/payment-requests/{pr['id']}").status_code == 404
    assert intruder.post(f"/api/shop/payment-requests/{pr['id']}/cancel").status_code == 404
    assert intruder.post(f"/api/shop/receipts/{number}/upload").status_code == 404


def test_state_changes_need_the_csrf_token(world):
    customer, cashier = Session("customer1"), Session("cashier")
    r = cashier.post("/api/shop/payment-requests", json={"amount": "5"}, csrf=False)
    assert r.status_code == 403 and r.json()["code"] == "csrf_failed"
    pr = cashier.post("/api/shop/payment-requests", json={"amount": "5"}).json()
    r = customer.post("/api/wallet/checkout/pay", json={"code": pr["code"], "expected_amount_minor": 500,
                                                        "confirm": True}, headers={"X-CSRF-Token": "forged"},
                      csrf=False)
    assert r.status_code == 403 and balance_of("customer1") == 10_000


def test_deactivated_user_is_signed_out_and_cannot_sign_in(world):
    customer, admin = Session("customer1"), Session("admin")
    cashier = Session("cashier")
    pr = cashier.post("/api/shop/payment-requests", json={"amount": "5"}).json()
    r = admin.post(f"/api/admin/users/{world['customers'][0]['id']}/deactivate")
    assert r.status_code == 200 and r.json()["active"] is False
    r = customer.post("/api/wallet/checkout/pay", json={"code": pr["code"], "expected_amount_minor": 500,
                                                        "confirm": True})
    assert r.status_code == 401
    r = Session().post("/api/auth/login", json={"username": "customer1", "password": PASSWORD})
    assert r.status_code == 401
    assert balance_of("customer1") == 10_000


def test_logout_ends_the_session(world):
    s = Session("customer1")
    assert s.post("/api/auth/logout").status_code == 200
    assert s.get("/api/wallet").status_code == 401


def test_wrong_password_and_rate_limit(world):
    s = Session()
    for _ in range(10):
        assert s.post("/api/auth/login", json={"username": "cashier", "password": "wrong-password"}).status_code == 401
    r = s.post("/api/auth/login", json={"username": "cashier", "password": PASSWORD})
    assert r.status_code == 401 and r.json()["code"] == "too_many_attempts"


def test_session_cookie_is_httponly_and_strict(world):
    s = Session()
    r = s.post("/api/auth/login", json={"username": "cashier", "password": PASSWORD})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    for header in ("content-security-policy", "x-frame-options", "x-content-type-options"):
        assert header in {k.lower() for k in r.headers}
