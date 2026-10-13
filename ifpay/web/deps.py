"""Request authentication (session cookie + CSRF header) and capability checks."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from .. import audit, db
from ..errors import DomainError, Forbidden
from ..security import CSRF_HEADER, SESSION_COOKIE, csrf_matches, has_capability, hash_token

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class Unauthenticated(DomainError):
    status = 401


@dataclass
class Principal:
    user: dict
    csrf_token: str


def current_principal(request: Request) -> Principal:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise Unauthenticated("not_signed_in", "Please sign in.")
    with db.connection() as conn:
        row = conn.execute(
            """SELECT s.csrf_token, u.* FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token_hash = %s AND s.revoked_at IS NULL AND s.expires_at > now()""",
            (hash_token(token),),
        ).fetchone()
    if row is None or not row["active"]:
        raise Unauthenticated("not_signed_in", "Your session has ended. Please sign in again.")
    csrf = row.pop("csrf_token")
    if request.method in UNSAFE_METHODS and not csrf_matches(csrf, request.headers.get(CSRF_HEADER)):
        raise Forbidden("csrf_failed", "Security check failed. Reload the page and try again.")
    return Principal(user=row, csrf_token=csrf)


def require(capability: str):
    def dependency(request: Request) -> dict:
        principal = current_principal(request)
        if not has_capability(principal.user, capability):
            with db.connection() as conn:
                audit.record(conn, actor=principal.user, action="access.denied", result="denied",
                             subject_type="endpoint", subject_id=request.url.path,
                             detail={"method": request.method, "capability": capability})
            raise Forbidden("forbidden", "You are not allowed to do this.")
        return principal.user

    return dependency
