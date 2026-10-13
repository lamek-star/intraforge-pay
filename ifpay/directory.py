"""Pilot users and the shop (merchant)."""

from __future__ import annotations

import psycopg

from . import audit, ledger
from .errors import Conflict, DomainError, Forbidden, NotFound
from .security import has_capability, hash_password, validate_new_password

ROLES = ("admin", "supervisor", "cashier", "customer")


def get_user(conn, user_id) -> dict:
    row = conn.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
    if row is None:
        raise NotFound("user_not_found", "User not found.")
    return row


def get_user_by_username(conn, username: str) -> dict | None:
    return conn.execute("SELECT * FROM users WHERE username = %s", (username.strip().lower(),)).fetchone()


def create_user(conn, *, actor: dict | None, username: str, display_name: str, role: str, password: str) -> dict:
    """`actor=None` only for bootstrap scripts run on the server itself; over the API an admin is required."""
    if actor is not None and not has_capability(actor, "users.manage"):
        raise Forbidden("forbidden", "You are not allowed to create users.")
    if role not in ROLES:
        raise DomainError("invalid_role", "Unknown role.")
    problem = validate_new_password(password)
    if problem:
        raise DomainError("weak_password", problem)
    username = username.strip().lower()
    display_name = display_name.strip()
    try:
        with conn.transaction():
            user = conn.execute(
                """INSERT INTO users (username, display_name, role, password_hash, created_by)
                   VALUES (%s, %s, %s, %s, %s) RETURNING *""",
                (username, display_name, role, hash_password(password), actor["id"] if actor else None),
            ).fetchone()
    except psycopg.errors.UniqueViolation as exc:
        raise Conflict("username_taken", "That username is already in use.") from exc
    except psycopg.errors.CheckViolation as exc:
        raise DomainError("invalid_user", "Username must be 3-40 lowercase letters, digits, '.', '_' or '-'; "
                                          "display name 1-80 characters.") from exc
    if role == "customer":
        ledger.open_customer_account(conn, user)
    audit.record(conn, actor=actor, action="user.create", subject_type="user", subject_id=user["id"],
                 detail={"username": username, "role": role})
    return user


def deactivate_user(conn, *, actor: dict, user_id) -> dict:
    if not has_capability(actor, "users.manage"):
        raise Forbidden("forbidden", "You are not allowed to manage users.")
    user = get_user(conn, user_id)
    if user["id"] == actor["id"]:
        raise DomainError("cannot_deactivate_self", "You cannot deactivate your own account.")
    conn.execute("UPDATE users SET active = false WHERE id = %s", (user_id,))
    conn.execute("UPDATE sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL", (user_id,))
    audit.record(conn, actor=actor, action="user.deactivate", subject_type="user", subject_id=user_id,
                 detail={"username": user["username"]})
    return get_user(conn, user_id)


def create_merchant(conn, *, name: str) -> dict:
    merchant = conn.execute("INSERT INTO merchants (name) VALUES (%s) RETURNING *", (name.strip(),)).fetchone()
    ledger.open_merchant_account(conn, merchant)
    audit.record(conn, actor=None, action="merchant.create", subject_type="merchant", subject_id=merchant["id"],
                 detail={"name": merchant["name"]})
    return merchant


def add_staff(conn, *, merchant_id, user_id) -> None:
    conn.execute("INSERT INTO merchant_staff (merchant_id, user_id) VALUES (%s, %s)", (merchant_id, user_id))


def merchant_for_staff(conn, user: dict) -> dict:
    row = conn.execute(
        """SELECT m.* FROM merchants m JOIN merchant_staff s ON s.merchant_id = m.id WHERE s.user_id = %s""",
        (user["id"],),
    ).fetchone()
    if row is None:
        raise Forbidden("not_shop_staff", "This account is not assigned to a shop.")
    return row


def the_merchant(conn) -> dict | None:
    """The single pilot shop."""
    return conn.execute("SELECT * FROM merchants ORDER BY created_at LIMIT 1").fetchone()


def public_user(user: dict) -> dict:
    return {
        "id": str(user["id"]),
        "username": user["username"],
        "display_name": user["display_name"],
        "role": user["role"],
        "active": user["active"],
    }
