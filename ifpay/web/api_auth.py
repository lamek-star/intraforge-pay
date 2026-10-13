from __future__ import annotations

import secrets
from datetime import timedelta

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from .. import audit, db, directory
from ..config import SIMULATED_NOTICE, get_settings
from ..security import (DUMMY_PASSWORD_HASH, SESSION_COOKIE, LoginRateLimiter, hash_token, new_session_token,
                        verify_password)
from .deps import Unauthenticated, current_principal

router = APIRouter(prefix="/api")
login_limiter = LoginRateLimiter()

HOME_FOR_ROLE = {"admin": "/admin", "supervisor": "/admin", "cashier": "/shop", "customer": "/wallet"}


class LoginBody(BaseModel):
    username: str = Field(max_length=40)
    password: str = Field(max_length=200)


@router.post("/auth/login")
def login(body: LoginBody, request: Request, response: Response):
    username = body.username.strip().lower()
    client_ip = request.client.host if request.client else "?"
    key = f"{username}|{client_ip}"
    if login_limiter.blocked(key):
        raise Unauthenticated("too_many_attempts", "Too many failed sign-in attempts. Wait a few minutes.")
    with db.connection() as conn:
        user = directory.get_user_by_username(conn, username)
        valid = verify_password(body.password, user["password_hash"] if user else DUMMY_PASSWORD_HASH)
        if not user or not valid or not user["active"]:
            login_limiter.record_failure(key)
            audit.record(conn, actor=None, action="auth.login", result="denied", subject_type="user",
                         subject_id=username, detail={"reason": "inactive" if user and valid else "bad_credentials"})
            conn.commit()
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
                        max_age=settings.session_hours * 3600, path="/")
    return {"user": directory.public_user(user), "csrf_token": csrf, "home": HOME_FOR_ROLE[user["role"]]}


@router.post("/auth/logout")
def logout(request: Request, response: Response):
    principal = current_principal(request)
    with db.connection() as conn:
        conn.execute("UPDATE sessions SET revoked_at = now() WHERE token_hash = %s",
                     (hash_token(request.cookies[SESSION_COOKIE]),))
        audit.record(conn, actor=principal.user, action="auth.logout", subject_type="user",
                     subject_id=principal.user["id"])
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(request: Request):
    principal = current_principal(request)
    return {"user": directory.public_user(principal.user), "csrf_token": principal.csrf_token,
            "home": HOME_FOR_ROLE[principal.user["role"]], "notice": SIMULATED_NOTICE}
