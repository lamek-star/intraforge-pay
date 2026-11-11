"""Supervisor / manager API: customer wallets, maker/checker credits, ledger, audit, and the IntraForge connection.

Who may call what is decided by IntraForge (ifpay.* capabilities of the signed-in member, see security.py)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from .. import allocations, db, directory, documents, ledger, refunds, rewards, runtime
from ..config import SIMULATED_NOTICE
from ..money import format_amount, parse_amount
from .deps import require

router = APIRouter(prefix="/api/admin")


class CustomerBody(BaseModel):
    username: str = Field(max_length=40)
    display_name: str = Field(max_length=80)
    password: str = Field(max_length=200)


class AllocationBody(BaseModel):
    customer_id: uuid.UUID
    amount: str | int
    reason: str = Field(default="", max_length=200)


class DecisionBody(BaseModel):
    note: str = Field(default="", max_length=200)


@router.get("/overview")
def overview(user: dict = Depends(require("admin.read"))):
    with db.connection() as conn:
        accounts = conn.execute(
            """SELECT b.*, u.display_name AS customer_name, m.name AS merchant_name
               FROM account_balances b
               LEFT JOIN users u ON u.id = b.customer_id
               LEFT JOIN merchants m ON m.id = b.merchant_id
               ORDER BY b.kind, b.code"""
        ).fetchall()
        report = ledger.conservation_report(conn)
        merchant = directory.the_merchant(conn)
    return {
        "notice": SIMULATED_NOTICE,
        "you": directory.public_user(user),
        "merchant": {"id": str(merchant["id"]), "name": merchant["name"]} if merchant else None,
        "conservation": {**report, **{k.replace("_minor", ""): format_amount(v) for k, v in report.items()
                                      if k.endswith("_minor")}},
        "accounts": [
            {"code": a["code"], "name": a["name"], "kind": a["kind"], "normal_side": a["normal_side"],
             "debits": format_amount(a["debits_minor"]), "credits": format_amount(a["credits_minor"]),
             "balance": format_amount(a["balance_minor"])}
            for a in accounts
        ],
    }


@router.get("/customers")
def customers(user: dict = Depends(require("admin.read"))):
    with db.connection() as conn:
        rows = conn.execute(
            """SELECT u.*, b.balance_minor FROM users u JOIN account_balances b ON b.customer_id = u.id
               WHERE u.role = 'customer' ORDER BY u.username"""
        ).fetchall()
    return [{**directory.public_user(r), "balance": format_amount(r["balance_minor"])} for r in rows]


@router.post("/customers")
def create_customer(body: CustomerBody, user: dict = Depends(require("users.manage"))):
    with db.connection() as conn:
        created = directory.create_user(conn, actor=user, username=body.username, display_name=body.display_name,
                                        role="customer", password=body.password)
    return directory.public_user(created)


@router.get("/users")
def users(user: dict = Depends(require("users.manage"))):
    with db.connection() as conn:
        return [directory.public_user(u) for u in conn.execute(
            "SELECT * FROM users WHERE role IN ('staff', 'customer') ORDER BY role, display_name")]


@router.post("/users/{user_id}/deactivate")
def deactivate(user_id: uuid.UUID, user: dict = Depends(require("users.manage"))):
    with db.connection() as conn:
        return directory.public_user(directory.deactivate_user(conn, actor=user, user_id=user_id))


@router.get("/allocations")
def list_allocations(user: dict = Depends(require("admin.read"))):
    with db.connection() as conn:
        rows = allocations.list_allocations(conn)
    return [
        {
            "id": str(r["id"]), "customer": r["customer_name"], "customer_username": r["customer_username"],
            "amount": format_amount(r["amount_minor"]), "reason": r["reason"], "status": r["status"],
            "requested_by": r["requested_by_username"], "requested_at": r["requested_at"].isoformat(),
            "decided_by": r["decided_by_username"],
            "decided_at": r["decided_at"].isoformat() if r["decided_at"] else None,
            "decision_note": r["decision_note"],
            "can_decide": r["status"] == "pending" and r["requested_by"] != user["id"],
        }
        for r in rows
    ]


@router.post("/allocations")
def request_allocation(body: AllocationBody, user: dict = Depends(require("allocations.request"))):
    with db.connection() as conn:
        row = allocations.request_allocation(conn, actor=user, customer_id=body.customer_id,
                                             amount_minor=parse_amount(body.amount), reason=body.reason)
    return {"id": str(row["id"]), "status": row["status"]}


@router.post("/allocations/{allocation_id}/approve")
def approve(allocation_id: uuid.UUID, body: DecisionBody, user: dict = Depends(require("allocations.decide"))):
    with db.connection() as conn:
        row = allocations.decide_allocation(conn, actor=user, allocation_id=allocation_id, approve=True,
                                            note=body.note)
    return {"id": str(row["id"]), "status": row["status"],
            "journal_transaction_id": str(row["journal_transaction_id"])}


@router.post("/allocations/{allocation_id}/reject")
def reject(allocation_id: uuid.UUID, body: DecisionBody, user: dict = Depends(require("allocations.decide"))):
    with db.connection() as conn:
        row = allocations.decide_allocation(conn, actor=user, allocation_id=allocation_id, approve=False,
                                            note=body.note)
    return {"id": str(row["id"]), "status": row["status"]}


@router.get("/transactions")
def transactions(user: dict = Depends(require("admin.read")), limit: int = Query(100, ge=1, le=500)):
    with db.connection() as conn:
        rows = conn.execute(
            """SELECT t.id, t.seq, t.kind, t.reference, t.memo, t.created_at, u.display_name AS created_by,
                      r.receipt_number,
                      json_agg(json_build_object('account', a.code, 'side', e.side, 'amount_minor', e.amount_minor)
                               ORDER BY e.id) AS entries
               FROM journal_transactions t
               JOIN users u ON u.id = t.created_by
               JOIN journal_entries e ON e.transaction_id = t.id
               JOIN ledger_accounts a ON a.id = e.account_id
               LEFT JOIN receipts r ON r.journal_transaction_id = t.id
               GROUP BY t.id, u.display_name, r.receipt_number
               ORDER BY t.seq DESC LIMIT %s""",
            (limit,),
        ).fetchall()
    return [
        {"id": str(r["id"]), "seq": r["seq"], "kind": r["kind"], "reference": r["reference"], "memo": r["memo"],
         "created_at": r["created_at"].isoformat(), "created_by": r["created_by"],
         "receipt_number": r["receipt_number"],
         "entries": [{"account": e["account"], "side": e["side"], "amount": format_amount(e["amount_minor"])}
                     for e in r["entries"]]}
        for r in rows
    ]


@router.get("/audit")
def audit_log(user: dict = Depends(require("audit.read")), limit: int = Query(200, ge=1, le=1000),
              action: str | None = Query(None, max_length=60)):
    with db.connection() as conn:
        rows = conn.execute(
            """SELECT * FROM audit_events WHERE (%s::text IS NULL OR action = %s)
               ORDER BY id DESC LIMIT %s""",
            (action, action, limit),
        ).fetchall()
    return [
        {"id": r["id"], "at": r["occurred_at"].isoformat(), "actor": r["actor_username"] or "system",
         "action": r["action"], "result": r["result"], "subject_type": r["subject_type"],
         "subject_id": r["subject_id"], "detail": r["detail"]}
        for r in rows
    ]


@router.get("/integration")
def integration(user: dict = Depends(require("admin.read"))):
    """Which IntraForge shop this Pay is installed in, and whether IntraForge confirmed it recently."""
    found = runtime.instance()
    status = documents.current_status()
    return {
        "instance": runtime.describe(found) if found else None,
        "status": ({"ok": status["ok"], "checked_at": status["checked_at"].isoformat(), "detail": status["detail"]}
                   if status else None),
    }


@router.post("/integration/verify")
def verify_integration(user: dict = Depends(require("admin.read"))):
    return documents.verify_now(actor=user)


# ---- cashback rate (maker/checker) ------------------------------------------------------------------------------------

class CashbackProposalBody(BaseModel):
    rate_percent: str = Field(max_length=6)  # "2" or "2.5"
    daily_cap: str | int
    reason: str = Field(max_length=200)


def _rate_bp(text: str) -> int:
    """'2.5' -> 250 basis points, exact (no floating point)."""
    from ..errors import DomainError

    value = text.strip()
    whole, _, frac = value.partition(".")
    if not whole.isdigit() or (frac and (not frac.isdigit() or len(frac) > 2)):
        raise DomainError("invalid_rate", "Enter the rate as a percentage, e.g. 2 or 2.5.")
    return int(whole) * 100 + int((frac + "00")[:2])


@router.get("/cashback")
def cashback(user: dict = Depends(require("admin.read"))):
    with db.connection() as conn:
        rate = rewards.describe_rate(rewards.current(conn))
        changes = rewards.list_changes(conn)
    return {
        "current": rate,
        "changes": [
            {
                "id": str(c["id"]), "rate_percent": f"{c['rate_bp'] / 100:.2f}", "daily_cap": format_amount(c["daily_cap_minor"]),
                "reason": c["reason"], "status": c["status"], "requested_by": c["requested_by_name"],
                "requested_at": c["requested_at"].isoformat(), "decided_by": c["decided_by_name"],
                "decided_at": c["decided_at"].isoformat() if c["decided_at"] else None, "decision_note": c["decision_note"],
                "can_decide": c["status"] == "pending" and c["requested_by"] != user["id"],
            }
            for c in changes
        ],
    }


@router.post("/cashback")
def propose_cashback(body: CashbackProposalBody, user: dict = Depends(require("allocations.request"))):
    with db.connection() as conn:
        row = rewards.propose(conn, actor=user, rate_bp=_rate_bp(body.rate_percent),
                              daily_cap_minor=parse_amount(body.daily_cap) if str(body.daily_cap).strip() not in ("0", "0.00") else 0,
                              reason=body.reason)
    return {"id": str(row["id"]), "status": row["status"]}


@router.post("/cashback/{change_id}/approve")
def approve_cashback(change_id: uuid.UUID, body: DecisionBody, user: dict = Depends(require("allocations.decide"))):
    with db.connection() as conn:
        row = rewards.decide(conn, actor=user, change_id=change_id, approve=True, note=body.note)
    return {"id": str(row["id"]), "status": row["status"]}


@router.post("/cashback/{change_id}/reject")
def reject_cashback(change_id: uuid.UUID, body: DecisionBody, user: dict = Depends(require("allocations.decide"))):
    with db.connection() as conn:
        row = rewards.decide(conn, actor=user, change_id=change_id, approve=False, note=body.note)
    return {"id": str(row["id"]), "status": row["status"]}


# ---- refunds: the checker's side ---------------------------------------------------------------------------------------

@router.get("/refunds")
def list_refunds_admin(user: dict = Depends(require("admin.read"))):
    with db.connection() as conn:
        merchant = directory.the_merchant(conn)
        rows = refunds.list_refunds(conn, merchant["id"]) if merchant else []
    return [{**refunds.public(r), "can_decide": r["status"] == "pending" and r["requested_by"] != user["id"]} for r in rows]


@router.post("/refunds/{refund_id}/approve")
def approve_refund(refund_id: uuid.UUID, body: DecisionBody, user: dict = Depends(require("allocations.decide"))):
    with db.connection() as conn:
        row = refunds.decide_refund(conn, actor=user, refund_id=refund_id, approve=True, note=body.note)
    return {"id": str(row["id"]), "status": row["status"],
            "returned_to_wallet": format_amount(row["amount_minor"] - row["cashback_reversed_minor"]),
            "cashback_taken_back": format_amount(row["cashback_reversed_minor"])}


@router.post("/refunds/{refund_id}/reject")
def reject_refund(refund_id: uuid.UUID, body: DecisionBody, user: dict = Depends(require("allocations.decide"))):
    with db.connection() as conn:
        row = refunds.decide_refund(conn, actor=user, refund_id=refund_id, approve=False, note=body.note)
    return {"id": str(row["id"]), "status": row["status"]}
