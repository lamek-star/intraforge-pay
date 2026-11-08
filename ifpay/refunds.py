"""Refunds of wallet payments, maker/checker.

Shop staff (checkout.create) ask for a refund of all or part of a paid payment; a DIFFERENT person who may approve
(allocations.decide, the supervisor) approves or rejects it. Only an approval posts to the journal: the shop's payable
pays the customer's wallet back. If the payment earned cashback, the refund takes back the matching share of it, so
nobody can pay, collect cashback, get a refund and keep the cashback. The approved refunds of a payment never exceed
what was paid. PostgreSQL re-checks every rule (0004_refunds_and_cashback.sql).
"""

from __future__ import annotations

from . import audit, ledger
from .errors import Conflict, DomainError, Forbidden, NotFound
from .money import format_amount
from .security import has_capability


def _payment_for_shop(conn, merchant_id, *, receipt_number: str | None = None, payment_request_id=None, lock: bool):
    lock_sql = " FOR UPDATE OF p" if lock else ""
    if receipt_number:
        row = conn.execute(
            f"""SELECT p.*, r.receipt_number FROM payment_requests p JOIN receipts r ON r.payment_request_id = p.id
                WHERE r.receipt_number = %s AND p.merchant_id = %s{lock_sql}""",
            (receipt_number.strip(), merchant_id),
        ).fetchone()
    else:
        row = conn.execute(
            f"""SELECT p.*, r.receipt_number FROM payment_requests p LEFT JOIN receipts r ON r.payment_request_id = p.id
                WHERE p.id = %s AND p.merchant_id = %s{lock_sql}""",
            (payment_request_id, merchant_id),
        ).fetchone()
    if row is None or row["status"] != "paid":
        raise NotFound("payment_not_found", "No paid wallet payment with that receipt in this shop.")
    return row


def _approved_total(conn, payment_request_id) -> int:
    return int(conn.execute(
        "SELECT COALESCE(SUM(amount_minor), 0)::bigint AS n FROM refunds WHERE payment_request_id = %s AND status = 'approved'",
        (payment_request_id,),
    ).fetchone()["n"])


def request_refund(conn, *, actor: dict, merchant: dict, receipt_number: str, amount_minor: int, reason: str) -> dict:
    if not has_capability(actor, "checkout.create"):
        raise Forbidden("forbidden", "Only shop staff can ask for a refund.")
    ledger.require_positive(amount_minor)
    reason = (reason or "").strip()
    if not reason or len(reason) > 200:
        raise DomainError("invalid_reason", "Say why the payment is refunded (up to 200 characters).")
    payment = _payment_for_shop(conn, merchant["id"], receipt_number=receipt_number, lock=True)
    pending = int(conn.execute(
        "SELECT COALESCE(SUM(amount_minor), 0)::bigint AS n FROM refunds WHERE payment_request_id = %s AND status = 'pending'",
        (payment["id"],),
    ).fetchone()["n"])
    remaining = payment["amount_minor"] - _approved_total(conn, payment["id"]) - pending
    if amount_minor > remaining:
        raise Conflict("refund_too_large",
                       f"At most {format_amount(max(remaining, 0))} IF Credits of this payment can still be refunded.")
    row = conn.execute(
        """INSERT INTO refunds (payment_request_id, amount_minor, reason, requested_by)
           VALUES (%s, %s, %s, %s) RETURNING *""",
        (payment["id"], amount_minor, reason, actor["id"]),
    ).fetchone()
    audit.record(conn, actor=actor, action="refund.request", subject_type="refund", subject_id=row["id"],
                 detail={"receipt": payment["receipt_number"], "amount": format_amount(amount_minor)})
    return row


def decide_refund(conn, *, actor: dict, refund_id, approve: bool, note: str = "") -> dict:
    if not has_capability(actor, "allocations.decide"):
        raise Forbidden("forbidden", "Only a supervisor can approve or reject a refund.")
    refund = conn.execute("SELECT * FROM refunds WHERE id = %s FOR UPDATE", (refund_id,)).fetchone()
    if refund is None:
        raise NotFound("refund_not_found", "Refund not found.")
    if refund["status"] != "pending":
        raise Conflict("already_decided", f"This refund was already {refund['status']}.")
    if refund["requested_by"] == actor["id"]:
        raise Forbidden("self_approval", "Maker/checker: you cannot approve or reject your own refund request.")
    note = (note or "").strip()[:200]
    if not approve:
        row = conn.execute(
            """UPDATE refunds SET status = 'rejected', decided_by = %s, decided_at = now(), decision_note = %s
               WHERE id = %s RETURNING *""",
            (actor["id"], note, refund_id),
        ).fetchone()
        audit.record(conn, actor=actor, action="refund.reject", subject_type="refund", subject_id=refund_id,
                     detail={"amount": format_amount(refund["amount_minor"])})
        return row

    payment = conn.execute("SELECT * FROM payment_requests WHERE id = %s FOR UPDATE",
                           (refund["payment_request_id"],)).fetchone()
    already = _approved_total(conn, payment["id"])
    if already + refund["amount_minor"] > payment["amount_minor"]:
        raise Conflict("refund_too_large", "This refund would return more than was paid.")

    # The matching share of the payment's cashback; the refund that completes the payment takes back what is left.
    award = conn.execute("SELECT * FROM cashback_awards WHERE payment_request_id = %s", (payment["id"],)).fetchone()
    reversed_cashback = 0
    if award is not None:
        taken_back = int(conn.execute(
            """SELECT COALESCE(SUM(cashback_reversed_minor), 0)::bigint AS n FROM refunds
               WHERE payment_request_id = %s AND status = 'approved'""",
            (payment["id"],),
        ).fetchone()["n"])
        if already + refund["amount_minor"] == payment["amount_minor"]:
            reversed_cashback = award["amount_minor"] - taken_back
        else:
            reversed_cashback = min(award["amount_minor"] * refund["amount_minor"] // payment["amount_minor"],
                                    award["amount_minor"] - taken_back)
    net = refund["amount_minor"] - reversed_cashback
    if net <= 0:  # only a tiny last refund of a payment with cashback; nothing would reach the wallet
        raise Conflict("refund_below_cashback",
                       "This refund is not larger than the cashback it must take back. Refund a larger amount.")

    customer_acct = ledger.customer_account(conn, payment["paid_by"])
    merchant_acct = ledger.merchant_account(conn, payment["merchant_id"])
    ledger.lock_account(conn, merchant_acct["id"])
    tx = ledger.post_transaction(
        conn,
        kind="refund",
        reference=f"REFUND-{refund['id']}",
        memo=f"Refund: {refund['reason']}" + (f" (cashback {format_amount(reversed_cashback)} taken back)" if reversed_cashback else ""),
        created_by=actor["id"],
        entries=[
            ledger.Entry(merchant_acct["id"], "debit", net),
            ledger.Entry(customer_acct["id"], "credit", net),
        ],
    )
    row = conn.execute(
        """UPDATE refunds SET status = 'approved', decided_by = %s, decided_at = now(), decision_note = %s,
                  cashback_reversed_minor = %s, journal_transaction_id = %s
           WHERE id = %s RETURNING *""",
        (actor["id"], note, reversed_cashback, tx["id"], refund_id),
    ).fetchone()
    audit.record(conn, actor=actor, action="refund.approve", subject_type="refund", subject_id=refund_id,
                 detail={"amount": format_amount(refund["amount_minor"]), "cashback_taken_back": format_amount(reversed_cashback),
                         "returned_to_wallet": format_amount(net), "journal_transaction": str(tx["id"])})
    return row


def list_refunds(conn, merchant_id) -> list[dict]:
    return conn.execute(
        """SELECT f.*, r.receipt_number, p.amount_minor AS payment_amount_minor, c.display_name AS customer_name,
                  q.display_name AS requested_by_name, d.display_name AS decided_by_name
           FROM refunds f
           JOIN payment_requests p ON p.id = f.payment_request_id
           LEFT JOIN receipts r ON r.payment_request_id = p.id
           JOIN users c ON c.id = p.paid_by
           JOIN users q ON q.id = f.requested_by
           LEFT JOIN users d ON d.id = f.decided_by
           WHERE p.merchant_id = %s
           ORDER BY f.requested_at DESC""",
        (merchant_id,),
    ).fetchall()


def public(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "receipt_number": row.get("receipt_number"),
        "amount": format_amount(row["amount_minor"]),
        "amount_minor": row["amount_minor"],
        "payment_amount": format_amount(row["payment_amount_minor"]) if row.get("payment_amount_minor") else None,
        "reason": row["reason"],
        "status": row["status"],
        "customer": row.get("customer_name"),
        "requested_by": row.get("requested_by_name"),
        "requested_at": row["requested_at"].isoformat(),
        "decided_by": row.get("decided_by_name"),
        "decided_at": row["decided_at"].isoformat() if row["decided_at"] else None,
        "decision_note": row["decision_note"],
        "cashback_taken_back": format_amount(row["cashback_reversed_minor"]),
        "returned_to_wallet": format_amount(row["amount_minor"] - row["cashback_reversed_minor"])
        if row["status"] == "approved" else None,
    }
