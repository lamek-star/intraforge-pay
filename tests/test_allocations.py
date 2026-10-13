"""Maker/checker allocation of simulated credits."""

import psycopg
import pytest
from conftest import Session, balance_of, journal_count

from ifpay import db


def _customer_id(username):
    with db.connection() as conn:
        return str(conn.execute("SELECT id FROM users WHERE username = %s", (username,)).fetchone()["id"])


def test_maker_cannot_approve_own_request_but_a_checker_can(world):
    admin, supervisor = Session("admin"), Session("supervisor")
    r = admin.post("/api/admin/allocations", json={"customer_id": _customer_id("customer1"), "amount": "20",
                                                   "reason": "Top-up for test"})
    assert r.status_code == 200
    alloc_id = r.json()["id"]
    before = journal_count("allocation")

    r = admin.post(f"/api/admin/allocations/{alloc_id}/approve", json={})
    assert r.status_code == 403 and r.json()["code"] == "self_approval"
    assert balance_of("customer1") == 10_000 and journal_count("allocation") == before

    listed = {a["id"]: a for a in admin.get("/api/admin/allocations").json()}
    assert listed[alloc_id]["can_decide"] is False
    assert {a["id"]: a for a in supervisor.get("/api/admin/allocations").json()}[alloc_id]["can_decide"] is True

    r = supervisor.post(f"/api/admin/allocations/{alloc_id}/approve", json={"note": "ok"})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert balance_of("customer1") == 12_000 and journal_count("allocation") == before + 1

    # A decision is final.
    r = supervisor.post(f"/api/admin/allocations/{alloc_id}/reject", json={})
    assert r.status_code == 409
    assert balance_of("customer1") == 12_000


def test_rejection_posts_nothing(world):
    admin, supervisor = Session("admin"), Session("supervisor")
    alloc_id = admin.post("/api/admin/allocations", json={"customer_id": _customer_id("customer2"),
                                                          "amount": "50"}).json()["id"]
    before = journal_count()
    assert supervisor.post(f"/api/admin/allocations/{alloc_id}/reject", json={"note": "no"}).json()["status"] == "rejected"
    assert journal_count() == before and balance_of("customer2") == 10_000
    assert supervisor.post(f"/api/admin/allocations/{alloc_id}/approve", json={}).status_code == 409


def test_the_database_itself_refuses_self_approval_and_second_decisions(world):
    admin_id = str(world["admin"]["id"])
    admin = Session("admin")
    alloc_id = admin.post("/api/admin/allocations", json={"customer_id": _customer_id("customer3"),
                                                          "amount": "5"}).json()["id"]
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.connection() as conn:
            conn.execute("UPDATE credit_allocations SET status = 'rejected', decided_by = %s, decided_at = now() "
                         "WHERE id = %s", (admin_id, alloc_id))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with db.connection() as conn:
            conn.execute("UPDATE credit_allocations SET amount_minor = 999 WHERE id = %s", (alloc_id,))


def test_only_admin_and_supervisor_may_allocate(world):
    cust_id = _customer_id("customer1")
    for who in ("cashier", "customer1"):
        s = Session(who)
        r = s.post("/api/admin/allocations", json={"customer_id": cust_id, "amount": "1000"})
        assert r.status_code == 403, who
    pending = Session("admin").post("/api/admin/allocations", json={"customer_id": cust_id, "amount": "1"}).json()
    for who in ("cashier", "customer1"):
        assert Session(who).post(f"/api/admin/allocations/{pending['id']}/approve", json={}).status_code == 403


def test_allocation_amount_limits(world):
    admin = Session("admin")
    for bad in ("0", "-5", "1000.01", "abc", "1.234"):
        r = admin.post("/api/admin/allocations", json={"customer_id": _customer_id("customer1"), "amount": bad})
        assert r.status_code == 400, bad
