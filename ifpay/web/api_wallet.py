"""Customer wallet API, and the shared receipt endpoint (object-level authorization in receipts.get_receipt)."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from pydantic import BaseModel, Field

from .. import audit, checkout, db, directory, ledger, receipts, rewards
from ..config import SIMULATED_NOTICE
from ..errors import DomainError
from ..money import format_amount
from ..security import hash_token
from .api_shop import receipt_payload, upload_receipt_in_background
from .deps import current_principal, require

router = APIRouter(prefix="/api")

_DESCRIPTIONS = {
    "purchase": "Purchase at {merchant}",
    "cashback": "Cashback",
    "refund": "Refund",
    "allocation": "Simulated credit allocation (approved)",
}


class CodeBody(BaseModel):
    code: str = Field(max_length=200)


class PayBody(BaseModel):
    code: str = Field(max_length=200)
    expected_amount_minor: int
    confirm: bool


class PasswordBody(BaseModel):
    current_password: str = Field(max_length=200)
    new_password: str = Field(max_length=200)


@router.post("/wallet/password")
def change_password(body: PasswordBody, request: Request):
    """The customer chooses their own password (required first when the shop gave them a one-time password)."""
    principal = current_principal(request, realm="customer")
    with db.connection() as conn:
        user = directory.change_own_password(conn, user=principal.user, current_password=body.current_password,
                                             new_password=body.new_password)
        # Other devices signed in with the old password are signed out; this one stays signed in.
        conn.execute("""UPDATE sessions SET revoked_at = now()
                        WHERE user_id = %s AND revoked_at IS NULL AND token_hash <> %s""",
                     (user["id"], hash_token(principal.token)))
    return {"user": directory.public_user(user)}


@router.get("/wallet")
def wallet(user: dict = Depends(require("wallet.read"))):
    with db.connection() as conn:
        account = ledger.customer_account(conn, user["id"])
        balance = ledger.balance_minor(conn, account["id"])
        history = conn.execute(
            """SELECT t.kind, t.reference, t.created_at, e.side, e.amount_minor,
                      r.receipt_number, m.name AS merchant_name
               FROM journal_entries e
               JOIN journal_transactions t ON t.id = e.transaction_id
               LEFT JOIN receipts r ON r.journal_transaction_id = t.id
               LEFT JOIN merchants m ON m.id = r.merchant_id
               WHERE e.account_id = %s
               ORDER BY t.seq DESC LIMIT 100""",
            (account["id"],),
        ).fetchall()
        cashback_earned = rewards.earned_total(conn, user["id"])
        rate = rewards.describe_rate(rewards.current(conn))
    return {
        "notice": SIMULATED_NOTICE,
        "customer": user["display_name"],
        "balance": format_amount(balance),
        "balance_minor": balance,
        "cashback_earned": format_amount(max(cashback_earned, 0)),
        "cashback_rate": rate,
        "history": [
            {
                "kind": h["kind"],
                "description": _DESCRIPTIONS.get(h["kind"], "Simulated credit allocation (approved)").format(
                    merchant=h["merchant_name"] or "the shop"),
                "amount": ("-" if h["side"] == "debit" else "+") + format_amount(h["amount_minor"]),
                "at": h["created_at"].isoformat(),
                "receipt_number": h["receipt_number"],
            }
            for h in history
        ],
    }


@router.post("/wallet/checkout/resolve")
def resolve(body: CodeBody, user: dict = Depends(require("wallet.pay"))):
    with db.connection() as conn:
        return {**checkout.resolve(conn, actor=user, code=body.code), "notice": SIMULATED_NOTICE}


@router.post("/wallet/checkout/pay")
def pay(body: PayBody, background: BackgroundTasks, user: dict = Depends(require("wallet.pay"))):
    if body.confirm is not True:
        raise DomainError("not_confirmed", "Confirm the payment first.")
    try:
        with db.connection() as conn:
            receipt, newly_paid = checkout.pay(conn, actor=user, code=body.code,
                                               expected_amount_minor=body.expected_amount_minor)
            payload = receipt_payload(conn, receipt)
            balance = ledger.balance_minor(conn, ledger.customer_account(conn, user["id"])["id"])
    except checkout.PaymentRefused as exc:
        # The payment transaction has been rolled back; the refusal is recorded on its own.
        with db.connection() as conn:
            audit.record(conn, actor=user, action="payment.refused", result="denied", subject_type="payment_code",
                         subject_id=body.code.split(".")[1] if body.code.count(".") == 2 else "",
                         detail={"code": exc.code, "message": exc.message})
        raise
    if newly_paid:
        upload_receipt_in_background(background, user, receipt["receipt_number"])
    return {"status": "paid" if newly_paid else "already_paid", "receipt": payload,
            "balance": format_amount(balance), "notice": SIMULATED_NOTICE}


@router.get("/receipts/{number}")
def get_receipt(number: str, request: Request):
    principal = current_principal(request)
    with db.connection() as conn:
        receipt = receipts.get_receipt(conn, actor=principal.user, number=number)
        return receipt_payload(conn, receipt)
