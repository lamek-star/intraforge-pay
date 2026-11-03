"""QR payment codes.

A code is `IFPAY1.<payment request id, hex>.<signature>`. The signature is an HMAC (key kept only on this server) over the
request id, merchant, amount and expiry, so a code cannot be altered or forged. The server still looks the request up
and decides everything; the code carries no authority on its own.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import re
import uuid

import segno

from .keys import signing_key
from .errors import DomainError

PREFIX = "IFPAY1"
_CODE = re.compile(r"^IFPAY1\.([0-9a-f]{32})\.([A-Za-z0-9_-]{43})$")


def _signature(request: dict) -> str:
    message = "|".join([
        uuid.UUID(str(request["id"])).hex,
        str(request["merchant_id"]),
        str(request["amount_minor"]),
        str(int(request["expires_at"].timestamp())),
    ]).encode()
    digest = hmac.new(signing_key(), message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def make_code(request: dict) -> str:
    return f"{PREFIX}.{uuid.UUID(str(request['id'])).hex}.{_signature(request)}"


def parse_code(code: str) -> tuple[uuid.UUID, str]:
    match = _CODE.match((code or "").strip())
    if not match:
        raise DomainError("invalid_code", "This is not an IntraForge Pay code.")
    return uuid.UUID(match.group(1)), match.group(2)


def signature_valid(request: dict, signature: str) -> bool:
    return hmac.compare_digest(_signature(request), signature)


def svg_data_uri(code: str) -> str:
    buffer = io.BytesIO()
    segno.make(code, error="m").save(buffer, kind="svg", scale=6, border=3, dark="#0F172A", light="#FFFFFF")
    return "data:image/svg+xml;base64," + base64.b64encode(buffer.getvalue()).decode()
