"""Maker/checker allocation of simulated IF Credits.

A maker requests an allocation; a different person (the checker) approves or rejects it. Only an approval posts to the
journal: Debit the issuance reserve, Credit the customer's simulated liability account. The database refuses a decision
by the requester (CHECK decided_by <> requested_by) and any second decision (trigger), whatever this code does.
"""

from __future__ import annotations

from . import audit, ledger
from .errors import Conflict, DomainError, Forbidden, NotFound
from .money import format_amount
from .security import has_capability


def request_allocation(conn, *, actor: dict, customer_id, amount_minor: int, reason: str) -> dict:
    if not has_capability(actor, "allocations.request"):
        raise Forbidden("forbidden", "You are not allowed to request credit allocations.")
    reason = (reason or "").strip() or "Pilot test allocation"
    if len(reason) > 200:
        raise DomainError("invalid_reason", "The reason is limited to 200 characters.")
    customer = conn.execute("SELECT * FROM users WHERE id = %s", (customer_id,)).fetchone()
    if customer is None or customer["role"] != "customer":
        raise NotFound("customer_not_found", "Customer not found.")
    if not customer["active"]:
        raise DomainError("customer_inactive", "This customer account is deactivated.")
    row = conn.execute(
        """INSERT INTO credit_allocations (customer_id, amount_minor, reason, requested_by)
           VALUES (%s, %s, %s, %s) RETURNING *""",
        (customer_id, amount_minor, reason, actor["id"]),
    ).fetchone()
    audit.record(conn, actor=actor, action="allocation.request", subject_type="credit_allocation", subject_id=row["id"],
                 detail={"customer": customer["username"], "amount": format_amount(amount_minor)})
    return row


def decide_allocation(conn, *, actor: dict, allocation_id, approve: bool, note: str = "") -> dict:
    if not has_capability(actor, "allocations.decide"):
        raise Forbidden("forbidden", "You are not allowed to approve or reject credit allocations.")
    alloc = conn.execute("SELECT * FROM credit_allocations WHERE id = %s FOR UPDATE", (allocation_id,)).fetchone()
    if alloc is None:
        raise NotFound("allocation_not_found", "Allocation request not found.")
    if alloc["status"] != "pending":
        raise Conflict("allocation_already_decided", f"This request was already {alloc['status']}.")
    if alloc["requested_by"] == actor["id"]:
        raise Forbidden("self_approval", "Maker/checker: you cannot approve or reject your own request.")

    note = (note or "").strip()[:200]
    if approve:
        customer_acct = ledger.customer_account(conn, alloc["customer_id"])
        reserve = ledger.issuance_reserve(conn)
        tx = ledger.post_transaction(
            conn,
            kind="allocation",
            reference=f"ALLOC-{alloc['id']}",
            memo=f"Simulated credit allocation: {alloc['reason']}",
            created_by=actor["id"],
            entries=[
                ledger.Entry(reserve["id"], "debit", alloc["amount_minor"]),
                ledger.Entry(customer_acct["id"], "credit", alloc["amount_minor"]),
            ],
        )
        row = conn.execute(
            """UPDATE credit_allocations SET status = 'approved', decided_by = %s, decided_at = now(),
                      decision_note = %s, journal_transaction_id = %s
               WHERE id = %s RETURNING *""",
            (actor["id"], note, tx["id"], allocation_id),
        ).fetchone()
    else:
        row = conn.execute(
            """UPDATE credit_allocations SET status = 'rejected', decided_by = %s, decided_at = now(), decision_note = %s
               WHERE id = %s RETURNING *""",
            (actor["id"], note, allocation_id),
        ).fetchone()
    audit.record(conn, actor=actor, action="allocation.approve" if approve else "allocation.reject",
                 subject_type="credit_allocation", subject_id=allocation_id,
                 detail={"amount": format_amount(alloc["amount_minor"]),
                         "journal_transaction": str(row["journal_transaction_id"] or "")})
    return row


def list_allocations(conn) -> list[dict]:
    return conn.execute(
        """SELECT a.*, c.username AS customer_username, c.display_name AS customer_name,
                  r.display_name AS requested_by_username, d.display_name AS decided_by_username
           FROM credit_allocations a
           JOIN users c ON c.id = a.customer_id
           JOIN users r ON r.id = a.requested_by
           LEFT JOIN users d ON d.id = a.decided_by
           ORDER BY a.requested_at DESC"""
    ).fetchall()
