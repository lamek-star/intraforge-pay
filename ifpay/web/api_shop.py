"""Shopkeeper (cashier) API."""

from __future__ import annotations

import os
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from pydantic import BaseModel

from .. import checkout, db, directory, documents, qr, receipts, tls
from ..config import SIMULATED_NOTICE, get_settings
from ..errors import DomainError, NotFound
from ..money import format_amount, parse_amount
from .deps import require

router = APIRouter(prefix="/api/shop")


class AmountBody(BaseModel):
    amount: str | int


def _tz() -> str:
    return os.environ.get("IFPAY_TIMEZONE", "UTC")


@router.get("")
def overview(user: dict = Depends(require("shop.read"))):
    with db.connection() as conn:
        merchant = directory.merchant_for_staff(conn, user)
        payable = conn.execute("SELECT balance_minor FROM account_balances WHERE merchant_id = %s",
                               (merchant["id"],)).fetchone()
        today = conn.execute(
            """SELECT r.receipt_number, r.amount_minor, r.issued_at, c.display_name AS customer_name,
                      (u.id IS NOT NULL) AS uploaded
               FROM receipts r
               JOIN users c ON c.id = r.customer_id
               LEFT JOIN intraforge_uploads u ON u.receipt_id = r.id AND u.kind = 'receipt'
               WHERE r.merchant_id = %s
                 AND (r.issued_at AT TIME ZONE %s)::date = (now() AT TIME ZONE %s)::date
               ORDER BY r.issued_at DESC""",
            (merchant["id"], _tz(), _tz()),
        ).fetchall()
    status = documents.current_status()
    return {
        "notice": SIMULATED_NOTICE,
        "merchant": {"id": str(merchant["id"]), "name": merchant["name"], "active": merchant["active"]},
        "cashier": user["display_name"],
        "intraforge": {"ok": bool(status and status["ok"]),
                       "checked_at": status["checked_at"].isoformat() if status else None},
        "total_simulated_sales": format_amount(int(payable["balance_minor"])),
        "today_total": format_amount(sum(int(r["amount_minor"]) for r in today)),
        "today": [
            {"receipt_number": r["receipt_number"], "amount": format_amount(r["amount_minor"]),
             "issued_at": r["issued_at"].isoformat(), "customer": r["customer_name"],
             "uploaded": r["uploaded"]}
            for r in today
        ],
    }


@router.post("/payment-requests")
def create_payment_request(body: AmountBody, user: dict = Depends(require("checkout.create"))):
    amount_minor = parse_amount(body.amount)
    verified = documents.merchant_verified(actor=user)
    with db.connection() as conn:
        row = checkout.create_payment_request(conn, actor=user, amount_minor=amount_minor, merchant_verified=verified,
                                              ttl_seconds=get_settings().qr_ttl_seconds)
        info = checkout.describe(conn, row)
    code = qr.make_code(row)
    return {**info, "code": code, "qr_svg": qr.svg_data_uri(code), "notice": SIMULATED_NOTICE}


@router.get("/payment-requests/{request_id}")
def payment_request_status(request_id: uuid.UUID, user: dict = Depends(require("shop.read"))):
    with db.connection() as conn:
        return checkout.describe(conn, checkout.get_for_shop(conn, actor=user, request_id=request_id))


@router.post("/payment-requests/{request_id}/cancel")
def cancel_payment_request(request_id: uuid.UUID, user: dict = Depends(require("checkout.create"))):
    with db.connection() as conn:
        return checkout.describe(conn, checkout.cancel(conn, actor=user, request_id=request_id))


# The signed Android wallet app shipped with Pay (ifpay/static/download/).
APP_DOWNLOAD = "static/download/IFCreditsWallet.apk"


@router.get("/pairing")
def pairing(user: dict = Depends(require("shop.read"))):
    """The "connect to this shop" code for the customer app: shop name and id, IntraForge's HTTPS addresses with
    Pay's path, and IntraForge's certificate authority with its pin. Not secret (it is shown on the shop screen), but
    it is what the app trusts."""
    settings = get_settings()
    urls = tls.shop_urls(settings.public_urls, settings.base_path)
    if not urls or settings.ca_cert_file is None or not settings.ca_cert_file.is_file():
        raise DomainError("phones_not_set_up", "Phones are not set up for this shop yet: IntraForge's address for "
                                               "phones (IFPAY_PUBLIC_URLS) or its certificate authority "
                                               "(IFPAY_CA_CERT) is missing. See the manual, section 'Phones'.",
                          status=503)
    with db.connection() as conn:
        merchant = directory.merchant_for_staff(conn, user)
    code = tls.pairing_code(shop_name=merchant["name"], merchant_id=str(merchant["id"]), urls=urls,
                            ca=tls.load_ca(settings.ca_cert_file))
    # Step 1 for a customer: install the wallet app from this shop's own IntraForge (the same addresses).
    app_url = urls[0] + "/" + APP_DOWNLOAD
    return {"shop": merchant["name"], "code": code, "qr_svg": qr.svg_data_uri(code), "urls": urls,
            "app_url": app_url, "app_qr_svg": qr.svg_data_uri(app_url)}


@router.post("/receipts/{number}/upload")
def upload_receipt(number: str, user: dict = Depends(require("receipts.upload"))):
    row = documents.upload_receipt(actor=user, receipt_number=number)
    return {"intraforge_file_id": str(row["intraforge_file_id"]), "filename": row["filename"],
            "uploaded_at": row["uploaded_at"].isoformat()}


@router.post("/receipts/{number}/documents")
async def upload_document(number: str, request: Request, user: dict = Depends(require("receipts.upload"))):
    """Raw body upload: Content-Type is the document type, X-Filename the original name."""
    limit = get_settings().max_document_bytes
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise DomainError("document_too_large", "The document is larger than the pilot limit.", status=413)
    content = b""
    async for chunk in request.stream():
        content += chunk
        if len(content) > limit:
            raise DomainError("document_too_large", "The document is larger than the pilot limit.", status=413)
    from starlette.concurrency import run_in_threadpool

    row = await run_in_threadpool(
        documents.upload_supporting_document, actor=user, receipt_number=number,
        filename=request.headers.get("x-filename", "document"), mime_type=request.headers.get("content-type", ""),
        content=content,
    )
    return {"intraforge_file_id": str(row["intraforge_file_id"]), "filename": row["filename"]}


def upload_receipt_in_background(background: BackgroundTasks, user: dict, number: str) -> None:
    """After a payment commits, the shop server stores its receipt in IntraForge on its own behalf (`system=True`:
    the paying customer has no upload capability and does not need one). A failure is logged and can be retried."""

    def task():
        try:
            documents.upload_receipt(actor=user, receipt_number=number, system=True)
        except Exception:  # noqa: BLE001 -- recorded in the audit log by documents.upload_receipt
            pass

    background.add_task(task)


def receipt_payload(conn, receipt: dict) -> dict:
    return receipts.public_receipt(receipt, receipts.uploads_for(conn, receipt["id"]))


__all__ = ["router", "upload_receipt_in_background", "receipt_payload", "NotFound"]
