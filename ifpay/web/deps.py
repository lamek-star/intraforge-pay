"""Who is asking: an IntraForge staff session or a Pay customer session, plus CSRF and capability checks.

* Shop staff open Pay from IntraForge (Apps > Open). IntraForge sets an HttpOnly cookie ``ifapp_<slot>`` on Pay's path;
  every request presents it to IntraForge (``sessions/introspect``), which answers with the person and their ifpay.*
  capabilities in this shop *right now* -- a revoked role, a removed member or an IntraForge log-out ends access on the
  next request. The CSRF token for staff requests is the one IntraForge derives for that session.
* Customers sign in to their Pay wallet with their own username and password (cookie ``ifpay_session``).

A browser may hold both (a cashier who also has a wallet); each endpoint uses whichever holds its capability.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from .. import audit, db, directory, runtime
from ..errors import DomainError, Forbidden
from ..platform import IntegrationError
from ..security import (CSRF_HEADER, SESSION_COOKIE, csrf_matches, has_capability, hash_token,
                        staff_capabilities)

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class Unauthenticated(DomainError):
    status = 401

    def __init__(self, code: str, message: str):
        super().__init__(code, message)
        found = runtime.instance()
        # Where a member of shop staff signs in: IntraForge opens Pay again for them (after its own sign-in page).
        self.extra = {"intraforge_open": f"/service-apps/{found.app_id}/open"} if found else {}


@dataclass
class Principal:
    user: dict
    csrf_token: str
    kind: str  # "staff" or "customer"
    token: str


def staff_principal(request: Request) -> Principal | None:
    if hasattr(request.state, "staff"):
        return request.state.staff
    principal = None
    found = runtime.instance()
    token = request.cookies.get(found.cookie_name) if found else None
    if token:
        try:
            session = runtime.client().introspect(token)
        except IntegrationError as exc:
            raise DomainError("intraforge_unavailable", "IntraForge could not confirm your sign-in right now. Try "
                                                        "again in a moment.", status=503) from exc
        if session is not None:
            with db.connection() as conn:
                merchant = directory.the_merchant(conn)
                user = directory.sync_staff(conn, intraforge_user_id=session.user_id,
                                            display_name=session.display_name,
                                            capabilities=staff_capabilities(session.capabilities), merchant=merchant)
            user["email"] = session.email
            user["intraforge_capabilities"] = sorted(session.capabilities)
            principal = Principal(user=user, csrf_token=session.csrf_token, kind="staff", token=token)
    request.state.staff = principal
    return principal


def customer_principal(request: Request) -> Principal | None:
    if hasattr(request.state, "customer"):
        return request.state.customer
    principal = None
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        with db.connection() as conn:
            row = conn.execute(
                """SELECT s.csrf_token, u.* FROM sessions s JOIN users u ON u.id = s.user_id
                   WHERE s.token_hash = %s AND s.revoked_at IS NULL AND s.expires_at > now()""",
                (hash_token(token),),
            ).fetchone()
        if row is not None and row["active"] and row["role"] == "customer":
            csrf = row.pop("csrf_token")
            principal = Principal(user=row, csrf_token=csrf, kind="customer", token=token)
    request.state.customer = principal
    return principal


def _check_csrf(request: Request, principal: Principal) -> None:
    if request.method in UNSAFE_METHODS and not csrf_matches(principal.csrf_token, request.headers.get(CSRF_HEADER)):
        raise Forbidden("csrf_failed", "Security check failed. Reload the page and try again.")


def current_principal(request: Request, *, realm: str | None = None) -> Principal:
    """The signed-in person: staff first, then customer (or only the given realm)."""
    resolvers = {"staff": staff_principal, "customer": customer_principal}
    for name in ([realm] if realm else ["staff", "customer"]):
        principal = resolvers[name](request)
        if principal is not None:
            _check_csrf(request, principal)
            return principal
    raise Unauthenticated("not_signed_in", "Please sign in.")


def require(capability: str):
    def dependency(request: Request) -> dict:
        candidates = [p for p in (staff_principal(request), customer_principal(request)) if p is not None]
        if not candidates:
            raise Unauthenticated("not_signed_in", "Please sign in.")
        for principal in candidates:
            if has_capability(principal.user, capability):
                _check_csrf(request, principal)
                return principal.user
        with db.connection() as conn:
            audit.record(conn, actor=candidates[0].user, action="access.denied", result="denied",
                         subject_type="endpoint", subject_id=request.url.path,
                         detail={"method": request.method, "capability": capability})
        raise Forbidden("forbidden", "You are not allowed to do this. Ask the shop owner for a Pay role in "
                                     "IntraForge (Members > Roles).")

    return dependency
