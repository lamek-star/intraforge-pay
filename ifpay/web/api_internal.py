"""Internal API: IntraForge asks Pay, on behalf of another application in the same shop, to take a payment.

Only IntraForge can call it. The caller must present this deployment's own service token -- the one secret only Pay and
IntraForge hold -- and IntraForge's proxy refuses /apps/<slot>/internal/ from outside, so the path is not reachable from
a browser at all. IntraForge has already decided that the application is in the same organization and that the person
holds ifpay.checkout in this shop; Pay checks the capability again and records the person as the request's creator.

It also opens customer wallets for another application (IntraForge ADR-0022: the Movie Shop's owner approves a portal
customer and adds them to IF Pay). The approving person must hold ifpay.customers.manage; the wallet remembers the
application and record it was opened for, and only that application can look it up again.

A request made this way is an ordinary payment request: the customer scans it, sees the shop, the amount and what it is
for, and confirms in their own wallet. Pay answers a status question only to the application that made the request.
"""

from __future__ import annotations

import hmac
import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from .. import checkout, db, directory, documents, ledger, qr, runtime
from ..config import SIMULATED_NOTICE, get_settings
from ..errors import DomainError, NotFound
from ..money import format_amount
from ..platform import TOKEN_HEADER
from ..security import staff_capabilities

router = APIRouter(prefix="/internal", include_in_schema=False)


def require_intraforge(request: Request) -> None:
    expected = get_settings().service_token
    presented = request.headers.get(TOKEN_HEADER, "")
    if not expected or not presented or not hmac.compare_digest(presented.encode(), expected.encode()):
        raise DomainError("unauthorized", "Not allowed.", status=401)
    if runtime.instance() is None or not db.is_open():
        raise DomainError("not_ready", "IntraForge Pay is starting, or is not installed in a shop yet.", status=503)


class Person(BaseModel):
    id: uuid.UUID
    display_name: str = Field(default="", max_length=200)
    capabilities: list[str] = Field(default_factory=list, max_length=50)


class Source(BaseModel):
    app_id: uuid.UUID
    app_name: str = Field(min_length=1, max_length=200)
    external_id: str = Field(min_length=1, max_length=120)


class CreateBody(BaseModel):
    actor: Person
    amount_minor: int = Field(gt=0)
    reference: str = Field(min_length=1, max_length=120)
    source: Source


def _public(conn, row: dict) -> dict:
    return {**checkout.describe(conn, row), "notice": SIMULATED_NOTICE}


@router.post("/payment-requests", dependencies=[Depends(require_intraforge)])
def create(body: CreateBody):
    caps = frozenset(body.actor.capabilities)
    if "ifpay.checkout" not in caps:
        raise DomainError("forbidden", "This person may not take IntraForge Pay payments in this shop.", status=403)
    if body.amount_minor > 100_000:
        raise DomainError("amount_too_large", f"IntraForge Pay takes at most {format_amount(100_000)} IF Credits "
                                              "in one payment.", status=422)
    with db.connection() as conn:
        merchant = directory.the_merchant(conn)
        actor = directory.sync_staff(conn, intraforge_user_id=str(body.actor.id),
                                     display_name=body.actor.display_name or "Staff",
                                     capabilities=staff_capabilities(caps), merchant=merchant)
    verified = documents.merchant_verified(actor=actor)
    with db.connection() as conn:
        row = checkout.create_payment_request(
            conn, actor=actor, amount_minor=body.amount_minor, merchant_verified=verified,
            ttl_seconds=get_settings().qr_ttl_seconds, reference=body.reference.strip(),
            source={"app_id": str(body.source.app_id), "app_name": body.source.app_name.strip(),
                    "external_id": body.source.external_id})
        out = _public(conn, row)
    code = qr.make_code(row)
    return {**out, "code": code, "qr_svg": qr.svg_data_uri(code)}


@router.get("/payment-requests/{request_id}", dependencies=[Depends(require_intraforge)])
def status(request_id: uuid.UUID, source_app_id: uuid.UUID):
    with db.connection() as conn:
        row = conn.execute("SELECT * FROM payment_requests WHERE id = %s AND source_app_id = %s",
                           (request_id, source_app_id)).fetchone()
        if row is None:
            # Unknown, or made by another application: the same answer, so ids cannot be probed.
            raise NotFound("not_found", "No such payment request.")
        return _public(conn, row)


class CustomerBody(BaseModel):
    actor: Person
    username: str = Field(min_length=1, max_length=40)
    display_name: str = Field(min_length=1, max_length=80)
    source: Source


def _customer(conn, user: dict) -> dict:
    account = ledger.customer_account(conn, user["id"])
    return {
        **directory.public_user(user),
        "balance_minor": ledger.balance_minor(conn, account["id"]),
        "wallet_path": get_settings().cookie_path() + "wallet",
    }


@router.post("/customers", dependencies=[Depends(require_intraforge)])
def create_customer(body: CustomerBody):
    caps = frozenset(body.actor.capabilities)
    if "ifpay.customers.manage" not in caps:
        raise DomainError("forbidden", "This person may not open IntraForge Pay wallets in this shop.", status=403)
    with db.connection() as conn:
        merchant = directory.the_merchant(conn)
        actor = directory.sync_staff(conn, intraforge_user_id=str(body.actor.id),
                                     display_name=body.actor.display_name or "Staff",
                                     capabilities=staff_capabilities(caps), merchant=merchant)
        user, password = directory.create_customer_for_app(
            conn, actor=actor, username=body.username, display_name=body.display_name,
            source={"app_id": str(body.source.app_id), "app_name": body.source.app_name.strip(),
                    "external_id": body.source.external_id})
        out = _customer(conn, user)
    # The first password exists only in this answer; Pay keeps its hash.
    return {**out, "created": password is not None, "first_password": password}


@router.get("/customers/{user_id}", dependencies=[Depends(require_intraforge)])
def customer(user_id: uuid.UUID, source_app_id: uuid.UUID):
    with db.connection() as conn:
        user = conn.execute("SELECT * FROM users WHERE id = %s AND source_app_id = %s AND role = 'customer'",
                            (user_id, source_app_id)).fetchone()
        if user is None:
            # Unknown, or opened for another application: the same answer.
            raise NotFound("not_found", "No such customer.")
        return _customer(conn, user)
