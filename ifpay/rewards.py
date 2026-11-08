"""Cashback: the shop gives back a share of each wallet payment, into the customer's wallet.

The rate and the daily cap per customer change only by maker/checker: someone proposes (allocations.request), a
different person approves (allocations.decide, the supervisor). The rate in force is the latest approved proposal; with
none, there is no cashback. The cashback is paid from the shop's own payable (Debit merchant payable, Credit customer
liability), so no credit is created and the ledger conservation still holds. PostgreSQL re-checks the rules
(0004_refunds_and_cashback.sql).
"""

from __future__ import annotations

from . import audit, ledger
from .errors import Conflict, DomainError, Forbidden, NotFound
from .money import format_amount
from .security import has_capability

MAX_RATE_BP = 2000  # 20 %
MAX_DAILY_CAP_MINOR = 100000  # 1,000.00 per customer per day


def current(conn) -> dict | None:
    """The approved rate in force, or None (no cashback)."""
    return conn.execute(
        """SELECT * FROM cashback_rate_changes WHERE status = 'approved'
           ORDER BY decided_at DESC, requested_at DESC LIMIT 1"""
    ).fetchone()


def describe_rate(row: dict | None) -> dict:
    if row is None or row["rate_bp"] == 0:
        return {"active": False, "rate_bp": 0, "rate_percent": "0.00", "daily_cap": format_amount(0), "daily_cap_minor": 0}
    return {
        "active": True,
        "rate_bp": row["rate_bp"],
        "rate_percent": f"{row['rate_bp'] / 100:.2f}",
        "daily_cap": format_amount(row["daily_cap_minor"]),
        "daily_cap_minor": row["daily_cap_minor"],
        "approved_at": row["decided_at"].isoformat() if row["decided_at"] else None,
    }


def propose(conn, *, actor: dict, rate_bp: int, daily_cap_minor: int, reason: str) -> dict:
    if not has_capability(actor, "allocations.request"):
        raise Forbidden("forbidden", "You are not allowed to propose a cashback rate.")
    if not isinstance(rate_bp, int) or isinstance(rate_bp, bool) or not 0 <= rate_bp <= MAX_RATE_BP:
        raise DomainError("invalid_rate", "The cashback rate must be between 0 % and 20 %.")
    if not isinstance(daily_cap_minor, int) or isinstance(daily_cap_minor, bool) or not 0 <= daily_cap_minor <= MAX_DAILY_CAP_MINOR:
        raise DomainError("invalid_cap", f"The daily cap must be between 0 and {format_amount(MAX_DAILY_CAP_MINOR)}.")
    reason = (reason or "").strip()
    if not reason or len(reason) > 200:
        raise DomainError("invalid_reason", "Say why the rate changes (up to 200 characters).")
    row = conn.execute(
        """INSERT INTO cashback_rate_changes (rate_bp, daily_cap_minor, reason, requested_by)
           VALUES (%s, %s, %s, %s) RETURNING *""",
        (rate_bp, daily_cap_minor, reason, actor["id"]),
    ).fetchone()
    audit.record(conn, actor=actor, action="cashback.propose", subject_type="cashback_rate_change", subject_id=row["id"],
                 detail={"rate_percent": f"{rate_bp / 100:.2f}", "daily_cap": format_amount(daily_cap_minor)})
    return row


def decide(conn, *, actor: dict, change_id, approve: bool, note: str = "") -> dict:
    if not has_capability(actor, "allocations.decide"):
        raise Forbidden("forbidden", "Only a supervisor can approve or reject a cashback rate.")
    change = conn.execute("SELECT * FROM cashback_rate_changes WHERE id = %s FOR UPDATE", (change_id,)).fetchone()
    if change is None:
        raise NotFound("cashback_change_not_found", "Cashback rate change not found.")
    if change["status"] != "pending":
        raise Conflict("already_decided", f"This change was already {change['status']}.")
    if change["requested_by"] == actor["id"]:
        raise Forbidden("self_approval", "Maker/checker: you cannot approve or reject your own proposal.")
    row = conn.execute(
        """UPDATE cashback_rate_changes SET status = %s, decided_by = %s, decided_at = clock_timestamp(),
                  decision_note = %s
           WHERE id = %s RETURNING *""",
        ("approved" if approve else "rejected", actor["id"], (note or "").strip()[:200], change_id),
    ).fetchone()
    audit.record(conn, actor=actor, action="cashback.approve" if approve else "cashback.reject",
                 subject_type="cashback_rate_change", subject_id=change_id,
                 detail={"rate_percent": f"{change['rate_bp'] / 100:.2f}", "daily_cap": format_amount(change["daily_cap_minor"])})
    return row


def list_changes(conn) -> list[dict]:
    return conn.execute(
        """SELECT c.*, r.display_name AS requested_by_name, d.display_name AS decided_by_name
           FROM cashback_rate_changes c
           JOIN users r ON r.id = c.requested_by
           LEFT JOIN users d ON d.id = c.decided_by
           ORDER BY c.requested_at DESC"""
    ).fetchall()


def award(conn, *, payment_request: dict, customer: dict, merchant_acct: dict, customer_acct: dict) -> dict | None:
    """Called inside the payment's own transaction, after the purchase is posted. Returns the award, or None when there
    is no rate, the amount rounds to zero, or the customer has reached today's cap (UTC day)."""
    rate = current(conn)
    if rate is None or rate["rate_bp"] == 0 or rate["daily_cap_minor"] == 0:
        return None
    amount = payment_request["amount_minor"] * rate["rate_bp"] // 10000  # rounded down: never more than the rate
    earned_today = conn.execute(
        """SELECT COALESCE(SUM(amount_minor), 0)::bigint AS n FROM cashback_awards
           WHERE customer_id = %s AND awarded_at >= date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'""",
        (customer["id"],),
    ).fetchone()["n"]
    amount = min(amount, rate["daily_cap_minor"] - int(earned_today))
    if amount <= 0:
        return None
    tx = ledger.post_transaction(
        conn,
        kind="cashback",
        reference=f"CASHBACK-{payment_request['id']}",
        memo=f"Cashback {rate['rate_bp'] / 100:.2f} % on a wallet payment",
        created_by=customer["id"],
        entries=[
            ledger.Entry(merchant_acct["id"], "debit", amount),
            ledger.Entry(customer_acct["id"], "credit", amount),
        ],
    )
    row = conn.execute(
        """INSERT INTO cashback_awards (payment_request_id, customer_id, rate_change_id, rate_bp, amount_minor,
                                        journal_transaction_id)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
        (payment_request["id"], customer["id"], rate["id"], rate["rate_bp"], amount, tx["id"]),
    ).fetchone()
    audit.record(conn, actor=customer, action="cashback.award", subject_type="payment_request",
                 subject_id=payment_request["id"],
                 detail={"cashback": format_amount(amount), "rate_percent": f"{rate['rate_bp'] / 100:.2f}"})
    return row


def earned_total(conn, customer_id) -> int:
    """Cashback this customer has received, less what refunds took back."""
    row = conn.execute(
        """SELECT (SELECT COALESCE(SUM(amount_minor), 0) FROM cashback_awards WHERE customer_id = %s)
                - (SELECT COALESCE(SUM(f.cashback_reversed_minor), 0) FROM refunds f
                   JOIN payment_requests p ON p.id = f.payment_request_id
                   WHERE p.paid_by = %s AND f.status = 'approved') AS n""",
        (customer_id, customer_id),
    ).fetchone()
    return int(row["n"])
