"""QR checkout: the shop creates a payment request, the customer resolves the code, checks it, and confirms.

Payment authorization is decided here, from the customer's own session, their explicit confirmation of the exact amount,
and the journal. Uploading documents to IntraForge is a separate, later step and never authorizes anything.
"""

from __future__ import annotations

from datetime import timedelta

from . import audit, directory, ledger, qr, receipts
from .errors import Conflict, DomainError, Forbidden, NotFound
from .money import format_amount
from .security import has_capability


class PaymentRefused(DomainError):
    """A refusal that is also written to the audit log (in its own transaction, after the rollback)."""


def create_payment_request(conn, *, actor: dict, amount_minor: int, merchant_verified: bool, ttl_seconds: int,
                           reference: str | None = None, source: dict | None = None) -> dict:
    """`reference` says what the payment is for (shown to the customer); `source` = {app_id, app_name, external_id}
    names the IntraForge application that asked for it (IntraForge vouches for it), which alone may ask its status."""
    if not has_capability(actor, "checkout.create"):
        raise Forbidden("forbidden", "Only shop staff can create a payment QR.")
    merchant = directory.merchant_for_staff(conn, actor)
    if not merchant["active"]:
        raise Forbidden("merchant_inactive", "This shop is deactivated.")
    if not merchant_verified:
        raise DomainError("merchant_not_verified",
                          "The shop's IntraForge connection is not verified. Ask the administrator to check it.",
                          status=503)
    now = conn.execute("SELECT now() AS now").fetchone()["now"]
    source = source or {}
    row = conn.execute(
        """INSERT INTO payment_requests (merchant_id, amount_minor, created_by, created_at, expires_at, merchant_verified,
                                         reference, source_app_id, source_app_name, source_external_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        (merchant["id"], amount_minor, actor["id"], now, now + timedelta(seconds=ttl_seconds), merchant_verified,
         reference or None, source.get("app_id"), source.get("app_name"), source.get("external_id")),
    ).fetchone()
    detail = {"amount": format_amount(amount_minor), "expires_at": row["expires_at"].isoformat()}
    if reference:
        detail["reference"] = reference
    if source:
        detail["requested_by_app"] = source.get("app_name")
    audit.record(conn, actor=actor, action="checkout.create", subject_type="payment_request", subject_id=row["id"],
                 detail=detail)
    return row


def _load_by_code(conn, code: str, *, lock: bool) -> dict:
    request_id, signature = qr.parse_code(code)
    row = conn.execute(
        "SELECT * FROM payment_requests WHERE id = %s" + (" FOR UPDATE" if lock else ""), (request_id,)
    ).fetchone()
    if row is None or not qr.signature_valid(row, signature):
        raise NotFound("unknown_code", "This payment code is not valid.")
    return row


def _is_expired(conn, request: dict) -> bool:
    return conn.execute("SELECT %s <= now() AS expired", (request["expires_at"],)).fetchone()["expired"]


def describe(conn, request: dict) -> dict:
    merchant = conn.execute("SELECT * FROM merchants WHERE id = %s", (request["merchant_id"],)).fetchone()
    status = request["status"]
    if status == "pending" and _is_expired(conn, request):
        status = "expired"
    receipt = conn.execute("SELECT receipt_number FROM receipts WHERE payment_request_id = %s",
                           (request["id"],)).fetchone()
    return {
        "id": str(request["id"]),
        "merchant": {"id": str(merchant["id"]), "name": merchant["name"],
                     "verified": bool(request["merchant_verified"] and merchant["active"])},
        "amount": format_amount(request["amount_minor"]),
        "amount_minor": request["amount_minor"],
        "status": status,
        "created_at": request["created_at"].isoformat(),
        "expires_at": request["expires_at"].isoformat(),
        "receipt_number": receipt["receipt_number"] if receipt else None,
        "paid_at": request["paid_at"].isoformat() if request["paid_at"] else None,
        "reference": request["reference"],
        "requested_by_app": request["source_app_name"],
    }


def resolve(conn, *, actor: dict, code: str) -> dict:
    if not has_capability(actor, "wallet.pay"):
        raise Forbidden("forbidden", "Only customers can pay.")
    return describe(conn, _load_by_code(conn, code, lock=False))


def pay(conn, *, actor: dict, code: str, expected_amount_minor: int) -> tuple[dict, bool]:
    """Returns (receipt, newly_paid). Paying the same code twice returns the first receipt and charges nothing."""
    if not has_capability(actor, "wallet.pay"):
        raise Forbidden("forbidden", "Only customers can pay.")
    request = _load_by_code(conn, code, lock=True)  # row lock: concurrent confirmations queue here

    if request["status"] == "paid":
        if request["paid_by"] == actor["id"]:
            receipt = conn.execute("SELECT * FROM receipts WHERE payment_request_id = %s", (request["id"],)).fetchone()
            return receipt, False
        raise PaymentRefused("already_paid", "This QR code has already been paid.", status=409)
    if request["status"] == "cancelled":
        raise PaymentRefused("cancelled", "The shop cancelled this payment request.", status=409)
    if _is_expired(conn, request):
        raise PaymentRefused("expired", "This QR code has expired. Ask the shop for a new one.", status=410)
    if expected_amount_minor != request["amount_minor"]:
        raise PaymentRefused("amount_mismatch", "The amount you confirmed does not match the payment request.",
                             status=409)

    merchant = conn.execute("SELECT * FROM merchants WHERE id = %s", (request["merchant_id"],)).fetchone()
    if not merchant["active"] or not request["merchant_verified"]:
        raise PaymentRefused("merchant_not_verified", "This shop is not verified.", status=409)

    customer_acct = ledger.customer_account(conn, actor["id"])
    merchant_acct = ledger.merchant_account(conn, merchant["id"])
    ledger.lock_account(conn, customer_acct["id"])  # serialise this customer's payments
    balance = ledger.balance_minor(conn, customer_acct["id"])
    if balance < request["amount_minor"]:
        raise PaymentRefused("insufficient_balance",
                             f"Insufficient simulated balance: you have {format_amount(balance)} IF Credits.",
                             status=409)

    tx = ledger.post_transaction(
        conn,
        kind="purchase",
        reference=f"PAY-{request['id']}",
        memo=f"Simulated purchase at {merchant['name']}",
        created_by=actor["id"],
        entries=[
            ledger.Entry(customer_acct["id"], "debit", request["amount_minor"]),
            ledger.Entry(merchant_acct["id"], "credit", request["amount_minor"]),
        ],
    )
    conn.execute(
        """UPDATE payment_requests SET status = 'paid', paid_by = %s, paid_at = now(), journal_transaction_id = %s
           WHERE id = %s""",
        (actor["id"], tx["id"], request["id"]),
    )
    entries = conn.execute(
        """SELECT a.code, e.side, e.amount_minor FROM journal_entries e JOIN ledger_accounts a ON a.id = e.account_id
           WHERE e.transaction_id = %s ORDER BY e.id""",
        (tx["id"],),
    ).fetchall()
    receipt = receipts.issue_receipt(conn, payment_request=request, transaction=tx, customer=actor, merchant=merchant,
                                     entries=entries)
    audit.record(conn, actor=actor, action="payment.confirm", subject_type="journal_transaction", subject_id=tx["id"],
                 detail={"receipt": receipt["receipt_number"], "amount": format_amount(request["amount_minor"]),
                         "merchant": merchant["name"], "payment_request": str(request["id"])})
    return receipt, True


def cancel(conn, *, actor: dict, request_id) -> dict:
    if not has_capability(actor, "checkout.create"):
        raise Forbidden("forbidden", "Only shop staff can cancel a payment request.")
    merchant = directory.merchant_for_staff(conn, actor)
    request = conn.execute("SELECT * FROM payment_requests WHERE id = %s AND merchant_id = %s FOR UPDATE",
                           (request_id, merchant["id"])).fetchone()
    if request is None:
        raise NotFound("payment_request_not_found", "Payment request not found.")
    if request["status"] != "pending":
        raise Conflict("not_pending", f"This payment request is already {request['status']}.")
    row = conn.execute("UPDATE payment_requests SET status = 'cancelled' WHERE id = %s RETURNING *",
                       (request_id,)).fetchone()
    audit.record(conn, actor=actor, action="checkout.cancel", subject_type="payment_request", subject_id=request_id)
    return row


def get_for_shop(conn, *, actor: dict, request_id) -> dict:
    if not has_capability(actor, "shop.read"):
        raise Forbidden("forbidden", "Only shop staff can view payment requests.")
    merchant = directory.merchant_for_staff(conn, actor)
    request = conn.execute("SELECT * FROM payment_requests WHERE id = %s AND merchant_id = %s",
                           (request_id, merchant["id"])).fetchone()
    if request is None:
        raise NotFound("payment_request_not_found", "Payment request not found.")
    return request
