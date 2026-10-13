"""Receipt and supporting-document storage in the authorized IntraForge bucket, and the shop's verification status.

Nothing here can authorize, block or reverse a payment: a payment is final when its journal transaction commits, and an
upload that fails afterwards is simply retried later. No database transaction is held open across an HTTP call.
"""

from __future__ import annotations

import re
import threading
from collections import defaultdict

import psycopg
from psycopg.types.json import Jsonb

from . import audit, db, receipts
from .config import get_settings
from .errors import DomainError, Forbidden
from .intraforge import IntegrationError, client_from, sha256_hex
from .security import has_capability

ALLOWED_DOCUMENT_TYPES = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "text/plain": ".txt",
}
_receipt_locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")


# --- verification ------------------------------------------------------------------------------------------------------


def verify_now(*, actor: dict | None, transport=None) -> dict:
    """Ask IntraForge whether the credential is valid, is the configured Application, and can reach the bucket."""
    try:
        with db.connection() as conn:
            client = client_from(conn, transport=transport)
        with client:
            detail = client.verify()
        ok, result = True, {"ok": True, **detail}
    except IntegrationError as exc:
        ok, result = False, {"ok": False, "code": exc.code, "message": exc.message}
    with db.connection() as conn:
        conn.execute(
            """INSERT INTO integration_status (id, ok, checked_at, detail) VALUES (1, %s, now(), %s)
               ON CONFLICT (id) DO UPDATE SET ok = EXCLUDED.ok, checked_at = now(), detail = EXCLUDED.detail""",
            (ok, Jsonb(result)),
        )
        audit.record(conn, actor=actor, action="intraforge.verify", result="success" if ok else "error",
                     subject_type="intraforge_application", detail=result)
    return result


def current_status() -> dict | None:
    with db.connection() as conn:
        row = conn.execute(
            "SELECT ok, checked_at, detail, (now() - checked_at) < make_interval(secs => %s) AS fresh "
            "FROM integration_status WHERE id = 1",
            (get_settings().intraforge_verify_max_age_seconds,),
        ).fetchone()
    return row


def merchant_verified(*, actor: dict | None, transport=None) -> bool:
    """Verified within the last IFPAY_INTRAFORGE_VERIFY_MAX_AGE seconds, re-checking with IntraForge if older."""
    status = current_status()
    if status is not None and status["fresh"]:
        return bool(status["ok"])
    return bool(verify_now(actor=actor, transport=transport)["ok"])


# --- uploads -----------------------------------------------------------------------------------------------------------


def _require_upload(actor: dict) -> None:
    if not has_capability(actor, "receipts.upload"):
        raise Forbidden("forbidden", "You are not allowed to upload documents to IntraForge.")


def upload_receipt(*, actor: dict, receipt_number: str, system: bool = False, transport=None) -> dict:
    """Idempotent: a receipt already stored in IntraForge is not uploaded again.

    `system=True` is the shop server's own automatic upload right after a payment commits (never reachable from a
    request parameter); otherwise the actor needs receipts.upload and must be allowed to read this receipt."""
    if not system:
        _require_upload(actor)
    with _receipt_locks[receipt_number]:
        with db.connection() as conn:
            if system:
                receipt = conn.execute("SELECT * FROM receipts WHERE receipt_number = %s",
                                       (receipt_number,)).fetchone()
            else:
                receipt = receipts.get_receipt(conn, actor=actor, number=receipt_number)
            existing = conn.execute(
                "SELECT * FROM intraforge_uploads WHERE receipt_id = %s AND kind = 'receipt'", (receipt["id"],)
            ).fetchone()
            if existing:
                return existing
            client = client_from(conn, transport=transport)
        content = receipts.render_text(receipt)
        filename = f"{receipt['receipt_number']}.txt"
        try:
            with client:
                stored = client.upload(filename=filename, content=content, mime_type="text/plain")
        except IntegrationError as exc:
            _record_failure(None if system else actor, "intraforge.receipt.upload", receipt_number, exc)
            raise
        return _record_upload(actor=None if system else actor, receipt=receipt, kind="receipt", filename=filename,
                              mime_type="text/plain", content=content, stored=stored,
                              bucket_id=client.config.bucket_id)


def upload_supporting_document(*, actor: dict, receipt_number: str, filename: str, mime_type: str, content: bytes,
                               transport=None) -> dict:
    _require_upload(actor)
    mime_type = (mime_type or "").split(";")[0].strip().lower()
    if mime_type not in ALLOWED_DOCUMENT_TYPES:
        raise DomainError("unsupported_type", "Supporting documents may be PDF, PNG, JPEG or plain text.")
    if not content:
        raise DomainError("empty_document", "The document is empty.")
    if len(content) > get_settings().max_document_bytes:
        raise DomainError("document_too_large", "The document is larger than the pilot limit.", status=413)
    base = _SAFE_NAME.sub("_", (filename or "document").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]).strip(" .")[:80]
    base = base or "document"
    with db.connection() as conn:
        receipt = receipts.get_receipt(conn, actor=actor, number=receipt_number)
        client = client_from(conn, transport=transport)
    stored_name = f"{receipt['receipt_number']}-support-{base}"
    if not stored_name.lower().endswith(ALLOWED_DOCUMENT_TYPES[mime_type]):
        stored_name += ALLOWED_DOCUMENT_TYPES[mime_type]
    try:
        with client:
            stored = client.upload(filename=stored_name, content=content, mime_type=mime_type)
    except IntegrationError as exc:
        _record_failure(actor, "intraforge.document.upload", receipt_number, exc)
        raise
    return _record_upload(actor=actor, receipt=receipt, kind="supporting_document", filename=stored_name,
                          mime_type=mime_type, content=content, stored=stored, bucket_id=client.config.bucket_id)


def _record_upload(*, actor, receipt, kind, filename, mime_type, content, stored, bucket_id) -> dict:
    with db.connection() as conn:
        try:
            with conn.transaction():
                row = conn.execute(
                    """INSERT INTO intraforge_uploads (receipt_id, kind, filename, mime_type, size_bytes, sha256,
                                                       intraforge_file_id, bucket_id, uploaded_by)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
                    (receipt["id"], kind, filename, mime_type, len(content), sha256_hex(content), stored["id"],
                     bucket_id, actor["id"] if actor else None),
                ).fetchone()
        except psycopg.errors.UniqueViolation:
            return conn.execute("SELECT * FROM intraforge_uploads WHERE receipt_id = %s AND kind = 'receipt'",
                                (receipt["id"],)).fetchone()
        audit.record(conn, actor=actor, action=f"intraforge.{kind}.upload", subject_type="receipt",
                     subject_id=receipt["receipt_number"],
                     detail={"intraforge_file_id": str(stored["id"]), "filename": filename, "bucket_id": bucket_id,
                             "sha256": sha256_hex(content)})
    return row


def _record_failure(actor, action, receipt_number, exc: IntegrationError) -> None:
    with db.connection() as conn:
        audit.record(conn, actor=actor, action=action, result="error", subject_type="receipt",
                     subject_id=receipt_number, detail={"code": exc.code, "message": exc.message})
