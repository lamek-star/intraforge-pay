"""Sign-in. Customers sign in to their wallet here; shop staff never do -- they open Pay from IntraForge."""

from __future__ import annotations

import secrets
from datetime import timedelta

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, Field

from .. import audit, db, directory, runtime
from ..config import SIMULATED_NOTICE, get_settings
from ..platform import IntegrationError
from ..security import (DUMMY_PASSWORD_HASH, SESSION_COOKIE, LoginRateLimiter, has_capability, hash_token,
                        new_session_token, verify_password)
from .deps import Unauthenticated, current_principal

router = APIRouter(prefix="/api")
login_limiter = LoginRateLimiter()


def home_for(user: dict) -> str:
    """The page a person starts on (relative to Pay's base path)."""
    if user["role"] == "customer":
        return "wallet"
    if has_capability(user, "checkout.create"):
        return "shop"
    if has_capability(user, "admin.read"):
        return "admin"
    return "no-access"


class LoginBody(BaseModel):
    username: str = Field(max_length=40)
    password: str = Field(max_length=200)


@router.post("/auth/login")
def login(body: LoginBody, request: Request, response: Response):
    """Customer wallet sign-in."""
    username = body.username.strip().lower()
    client_ip = request.client.host if request.client else "?"
    key = f"{username}|{client_ip}"
    if login_limiter.blocked(key):
        raise Unauthenticated("too_many_attempts", "Too many failed sign-in attempts. Wait a few minutes.")
    with db.connection() as conn:
        user = directory.get_user_by_username(conn, username)
        stored = user["password_hash"] if user and user["password_hash"] else DUMMY_PASSWORD_HASH
        valid = verify_password(body.password, stored)
        if not user or not valid or not user["active"] or user["role"] != "customer":
            login_limiter.record_failure(key)
            reason = "bad_credentials"
            if user and valid:
                reason = "not_a_customer" if user["role"] != "customer" else "inactive"
            audit.record(conn, actor=None, action="auth.login", result="denied", subject_type="user",
                         subject_id=username, detail={"reason": reason})
            conn.commit()
            if reason == "not_a_customer":
                raise Unauthenticated("staff_use_intraforge", "Shop staff sign in through IntraForge: open "
                                                              "IntraForge Pay from IntraForge's Apps page.")
            raise Unauthenticated("bad_credentials", "Wrong username or password.")
        login_limiter.reset(key)
        token = new_session_token()
        csrf = secrets.token_urlsafe(24)
        settings = get_settings()
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, csrf_token, expires_at) VALUES (%s, %s, %s, now() + %s)",
            (hash_token(token), user["id"], csrf, timedelta(hours=settings.session_hours)),
        )
        audit.record(conn, actor=user, action="auth.login", subject_type="user", subject_id=user["id"])
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict", secure=settings.cookie_secure,
                        max_age=settings.session_hours * 3600, path=settings.cookie_path())
    return {"user": directory.public_user(user), "csrf_token": csrf, "home": home_for(user)}


@router.post("/auth/logout")
def logout(request: Request, response: Response, realm: str | None = Query(None, pattern="^(staff|customer)$")):
    principal = current_principal(request, realm=realm)
    settings = get_settings()
    if principal.kind == "staff":
        # Ends the IntraForge login this Pay session came from as well (IntraForge records auth.logout).
        try:
            runtime.client().sign_out(principal.token)
        except IntegrationError:
            pass
        with db.connection() as conn:
            audit.record(conn, actor=principal.user, action="auth.logout", subject_type="user",
                         subject_id=principal.user["id"], detail={"via": "intraforge"})
        found = runtime.instance()
        if found:
            response.delete_cookie(found.cookie_name, path=settings.cookie_path())
        return {"ok": True, "next": "/login"}
    with db.connection() as conn:
        conn.execute("UPDATE sessions SET revoked_at = now() WHERE token_hash = %s", (hash_token(principal.token),))
        audit.record(conn, actor=principal.user, action="auth.logout", subject_type="user",
                     subject_id=principal.user["id"])
    response.delete_cookie(SESSION_COOKIE, path=settings.cookie_path())
    return {"ok": True, "next": "login"}


@router.get("/me")
def me(request: Request, realm: str | None = Query(None, pattern="^(staff|customer)$")):
    principal = current_principal(request, realm=realm)
    found = runtime.instance()
    return {"user": directory.public_user(principal.user), "kind": principal.kind,
            "csrf_token": principal.csrf_token, "home": home_for(principal.user), "notice": SIMULATED_NOTICE,
            "shop": found.shop_name if found else None,
            "intraforge_open": f"/service-apps/{found.app_id}/open" if found else None}
