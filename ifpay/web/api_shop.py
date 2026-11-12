"""Shopkeeper (cashier) API."""

from __future__ import annotations

import os
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from pydantic import BaseModel

from .. import checkout, counters, db, directory, documents, qr, receipts, refunds, tls
from ..config import SIMULATED_NOTICE, get_settings
from ..errors import DomainError, NotFound
from ..money import format_amount, parse_amount
from .deps import require

router = APIRouter(prefix="/api/shop")


class AmountBody(BaseModel):
    amount: str | int
    counter_id: uuid.UUID | None = None


class CounterBody(BaseModel):
    name: str


def _tz(conn, asked: str | None = None) -> str:
    """The shop's time zone for "today": the browser's own (checked against PostgreSQL's list), else
    IFPAY_TIMEZONE, else UTC. Without it a shop east of UTC had its day turn over in the small hours."""
    if asked and len(asked) <= 64 and conn.execute("SELECT 1 FROM pg_timezone_names WHERE name = %s",
                                                   (asked,)).fetchone():
        return asked
    return os.environ.get("IFPAY_TIMEZONE", "UTC")


@router.get("")
def overview(tz: str | None = None, counter: uuid.UUID | None = None, user: dict = Depends(require("shop.read"))):
    with db.connection() as conn:
        merchant = directory.merchant_for_staff(conn, user)
        zone = _tz(conn, tz)
        payable = conn.execute("SELECT balance_minor FROM account_balances WHERE merchant_id = %s",
                               (merchant["id"],)).fetchone()
        all_today = conn.execute(
            """SELECT r.receipt_number, r.amount_minor, r.issued_at, c.display_name AS customer_name,
                      (u.id IS NOT NULL) AS uploaded, pr.counter_id, k.name AS counter_name
               FROM receipts r
               JOIN users c ON c.id = r.customer_id
               JOIN payment_requests pr ON pr.id = r.payment_request_id
               LEFT JOIN counters k ON k.id = pr.counter_id
               LEFT JOIN intraforge_uploads u ON u.receipt_id = r.id AND u.kind = 'receipt'
               WHERE r.merchant_id = %s
                 AND (r.issued_at AT TIME ZONE %s)::date = (now() AT TIME ZONE %s)::date
               ORDER BY r.issued_at DESC""",
            (merchant["id"], zone, zone),
        ).fetchall()
        by_counter: dict[str, dict] = {}
        for r in all_today:
            key = str(r["counter_id"]) if r["counter_id"] else ""
            entry = by_counter.setdefault(key, {"counter_id": key or None,
                                                "counter": r["counter_name"] or "No counter", "minor": 0, "sales": 0})
            entry["minor"] += int(r["amount_minor"])
            entry["sales"] += 1
        today = [r for r in all_today if counter is None or r["counter_id"] == counter]
    status = documents.current_status()
    return {
        "notice": SIMULATED_NOTICE,
        "merchant": {"id": str(merchant["id"]), "name": merchant["name"], "active": merchant["active"]},
        "cashier": user["display_name"],
        "intraforge": {"ok": bool(status and status["ok"]),
                       "checked_at": status["checked_at"].isoformat() if status else None},
        "total_simulated_sales": format_amount(int(payable["balance_minor"])),
        "time_zone": zone,
        "today_total": format_amount(sum(int(r["amount_minor"]) for r in all_today)),
        "counter_total": format_amount(sum(int(r["amount_minor"]) for r in today)) if counter else None,
        "counters_today": [
            {"counter_id": e["counter_id"], "counter": e["counter"], "total": format_amount(e["minor"]),
             "sales": e["sales"]}
            for e in sorted(by_counter.values(), key=lambda e: e["counter"])
        ],
        "today": [
            {"receipt_number": r["receipt_number"], "amount": format_amount(r["amount_minor"]),
             "issued_at": r["issued_at"].isoformat(), "customer": r["customer_name"],
             "uploaded": r["uploaded"], "counter": r["counter_name"]}
            for r in today
        ],
    }


@router.post("/payment-requests")
def create_payment_request(body: AmountBody, user: dict = Depends(require("checkout.create"))):
    amount_minor = parse_amount(body.amount)
    verified = documents.merchant_verified(actor=user)
    with db.connection() as conn:
        row = checkout.create_payment_request(conn, actor=user, amount_minor=amount_minor, merchant_verified=verified,
                                              ttl_seconds=get_settings().qr_ttl_seconds, counter_id=body.counter_id)
        info = checkout.describe(conn, row)
    code = qr.make_code(row)
    return {**info, "code": code, "qr_svg": qr.svg_data_uri(code), "notice": SIMULATED_NOTICE}


def _counter_json(row: dict) -> dict:
    return {"id": str(row["id"]), "name": row["name"], "active": row["active"]}


@router.get("/counters")
def list_counters(all: bool = False, user: dict = Depends(require("shop.read"))):
    with db.connection() as conn:
        return [_counter_json(r) for r in counters.list_counters(conn, actor=user, include_inactive=all)]


@router.post("/counters")
def create_counter(body: CounterBody, user: dict = Depends(require("users.manage"))):
    with db.connection() as conn:
        return _counter_json(counters.create_counter(conn, actor=user, name=body.name))


@router.post("/counters/{counter_id}/disable")
def disable_counter(counter_id: uuid.UUID, user: dict = Depends(require("users.manage"))):
    with db.connection() as conn:
        return _counter_json(counters.set_active(conn, actor=user, counter_id=counter_id, active=False))


@router.post("/counters/{counter_id}/enable")
def enable_counter(counter_id: uuid.UUID, user: dict = Depends(require("users.manage"))):
    with db.connection() as conn:
        return _counter_json(counters.set_active(conn, actor=user, counter_id=counter_id, active=True))


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


# ---- refunds: the maker's side (a supervisor approves them in the admin page) -----------------------------------------

class RefundBody(BaseModel):
    receipt_number: str
    amount: str | int
    reason: str


@router.get("/refunds")
def shop_refunds(user: dict = Depends(require("shop.read"))):
    with db.connection() as conn:
        merchant = directory.merchant_for_staff(conn, user)
        rows = refunds.list_refunds(conn, merchant["id"])
    return [refunds.public(r) for r in rows]


@router.post("/refunds")
def request_refund(body: RefundBody, user: dict = Depends(require("checkout.create"))):
    with db.connection() as conn:
        merchant = directory.merchant_for_staff(conn, user)
        row = refunds.request_refund(conn, actor=user, merchant=merchant, receipt_number=body.receipt_number[:40],
                                     amount_minor=parse_amount(body.amount), reason=body.reason[:200])
    return {"id": str(row["id"]), "status": row["status"], "amount": format_amount(row["amount_minor"])}
