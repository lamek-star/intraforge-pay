"""Receipts: one immutable receipt per successful purchase, identical for the customer and the shop.

The receipt body is a canonical JSON document; its SHA-256 is stored with it and printed on every rendering, so the copy
the customer sees, the copy the shop sees and the file uploaded to IntraForge can be compared byte for byte.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timezone

from psycopg.types.json import Jsonb

from .config import SIMULATED_NOTICE
from .errors import Forbidden, NotFound
from .money import format_amount
from .security import has_capability


def canonical_json(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def issue_receipt(conn, *, payment_request: dict, transaction: dict, customer: dict, merchant: dict,
                  entries: list[dict], cashback: dict | None = None) -> dict:
    seq = conn.execute("SELECT nextval('receipt_number_seq') AS n").fetchone()["n"]
    issued_at = transaction["created_at"].astimezone(timezone.utc)
    number = f"IFP-{issued_at:%Y%m%d}-{seq:06d}"
    body = {
        "notice": SIMULATED_NOTICE,
        "receipt_number": number,
        "issued_at": issued_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "merchant": {"id": str(merchant["id"]), "name": merchant["name"]},
        "customer": {"id": str(customer["id"]), "name": customer["display_name"]},
        "amount": format_amount(payment_request["amount_minor"]),
        "amount_minor": payment_request["amount_minor"],
        "unit": "IF Credits (simulated)",
        "payment_request_id": str(payment_request["id"]),
        "journal_transaction_id": str(transaction["id"]),
        "journal_reference": transaction["reference"],
        "journal_entries": [
            {"account": e["code"], "side": e["side"], "amount": format_amount(e["amount_minor"])} for e in entries
        ],
    }
    if cashback:
        body["cashback"] = {
            "amount": format_amount(cashback["amount_minor"]),
            "amount_minor": cashback["amount_minor"],
            "rate_percent": f"{cashback['rate_bp'] / 100:.2f}",
            "journal_transaction_id": str(cashback["journal_transaction_id"]),
        }
    digest = hashlib.sha256(canonical_json(body)).hexdigest()
    return conn.execute(
        """INSERT INTO receipts (receipt_number, journal_transaction_id, payment_request_id, customer_id, merchant_id,
                                 amount_minor, issued_at, body, body_sha256)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        (number, transaction["id"], payment_request["id"], customer["id"], merchant["id"],
         payment_request["amount_minor"], issued_at, Jsonb(body), digest),
    ).fetchone()


def get_receipt(conn, *, actor: dict, number: str) -> dict:
    """Object-level authorization: a customer sees only their own receipts, shop staff only their shop's."""
    receipt = conn.execute("SELECT * FROM receipts WHERE receipt_number = %s", (number,)).fetchone()
    # The same answer for "does not exist" and "not yours", so receipt numbers cannot be probed.
    not_found = NotFound("receipt_not_found", "Receipt not found.")
    if receipt is None:
        raise not_found
    if has_capability(actor, "receipts.read_all"):
        return receipt
    if has_capability(actor, "receipts.read_own") and receipt["customer_id"] == actor["id"]:
        return receipt
    if has_capability(actor, "receipts.read_merchant"):
        staff = conn.execute("SELECT 1 FROM merchant_staff WHERE user_id = %s AND merchant_id = %s",
                             (actor["id"], receipt["merchant_id"])).fetchone()
        if staff:
            return receipt
    if not any(has_capability(actor, c) for c in ("receipts.read_all", "receipts.read_own", "receipts.read_merchant")):
        raise Forbidden("forbidden", "You are not allowed to read receipts.")
    raise not_found


def uploads_for(conn, receipt_id) -> list[dict]:
    return conn.execute(
        "SELECT * FROM intraforge_uploads WHERE receipt_id = %s ORDER BY uploaded_at", (receipt_id,)
    ).fetchall()


def public_receipt(receipt: dict, uploads: list[dict] | None = None) -> dict:
    return {
        "receipt_number": receipt["receipt_number"],
        "body": receipt["body"],
        "sha256": receipt["body_sha256"],
        "uploads": [
            {
                "kind": u["kind"],
                "filename": u["filename"],
                "intraforge_file_id": str(u["intraforge_file_id"]),
                "uploaded_at": u["uploaded_at"].isoformat(),
            }
            for u in (uploads or [])
        ],
    }


def render_text(receipt: dict) -> bytes:
    """The plain-text receipt file stored in the IntraForge bucket."""
    b = receipt["body"]
    lines = [
        "=" * 60,
        f"  {SIMULATED_NOTICE}",
        "=" * 60,
        "IntraForge Pay — pilot receipt",
        "",
        f"Receipt number : {b['receipt_number']}",
        f"Issued (UTC)   : {b['issued_at']}",
        f"Shop           : {b['merchant']['name']}",
        f"Customer       : {b['customer']['name']}",
        f"Amount         : {b['amount']} {b['unit']}",
        f"Journal ref    : {b['journal_reference']}",
        f"Transaction id : {b['journal_transaction_id']}",
        "",
        "Journal entries:",
        *[f"  {e['side'].upper():6} {e['amount']:>10}  {e['account']}" for e in b["journal_entries"]],
        "",
        f"Receipt SHA-256 (canonical JSON): {receipt['body_sha256']}",
        "",
        f"  {SIMULATED_NOTICE}",
        "=" * 60,
        "",
        "Canonical receipt JSON:",
        canonical_json(b).decode("utf-8"),
        "",
    ]
    return "\n".join(lines).encode("utf-8")
