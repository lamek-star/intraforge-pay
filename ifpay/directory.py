"""People and the shop (merchant).

Shop staff are IntraForge people (role "staff"): IntraForge signs them in and decides their capabilities; Pay keeps a row
per person only so the journal, receipts and audit log can name them. Customers are Pay's own wallet accounts, created
by staff who hold ifpay.customers.manage. The shop is the IntraForge Pay instance this server is installed as."""

from __future__ import annotations

import re
import secrets
import uuid

import psycopg

from . import audit, ledger
from .errors import Conflict, DomainError, Forbidden, NotFound
from .security import has_capability, hash_password, validate_new_password, verify_password

ROLES = ("customer",)


def get_user(conn, user_id) -> dict:
    row = conn.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
    if row is None:
        raise NotFound("user_not_found", "User not found.")
    return row


def get_user_by_username(conn, username: str) -> dict | None:
    return conn.execute("SELECT * FROM users WHERE username = %s", (username.strip().lower(),)).fetchone()


def create_user(conn, *, actor: dict | None, username: str, display_name: str, role: str, password: str) -> dict:
    """Creates a customer wallet account. `actor=None` only for tests and scripts run on the server itself; over the
    API the actor needs users.manage (IntraForge capability ifpay.customers.manage)."""
    if actor is not None and not has_capability(actor, "users.manage"):
        raise Forbidden("forbidden", "You are not allowed to create users.")
    if role not in ROLES:
        raise DomainError("invalid_role", "Only customer accounts are created in Pay; shop staff are IntraForge "
                                          "members with a Pay role.")
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


# A first password a person can read out at the counter: no 0/O, 1/l/I.
_FIRST_PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
_USERNAME_UNSAFE = re.compile(r"[^a-z0-9._-]+")


def first_password() -> str:
    """Twelve characters in three groups ("k7mq-x2vd-9tna"), about 59 bits: used once, then replaced by the
    customer."""
    chars = [secrets.choice(_FIRST_PASSWORD_ALPHABET) for _ in range(12)]
    return "-".join("".join(chars[i:i + 4]) for i in (0, 4, 8))


def username_base(wanted: str) -> str:
    base = _USERNAME_UNSAFE.sub("-", wanted.strip().lower()).strip("-._")[:34]
    return base if len(base) >= 3 else f"customer-{base}".strip("-")[:34]


def create_customer_for_app(conn, *, actor: dict, username: str, display_name: str, source: dict
                            ) -> tuple[dict, str | None]:
    """Opens a customer wallet another application in this shop asked for (IntraForge decided the application is in
    the same shop; `actor` is the person who approved it there). Asking again for the same record returns the wallet
    already opened and no password. Otherwise Pay picks a free username starting with `username` and returns the
    one-time first password, which the customer must replace at their first sign-in."""
    if not has_capability(actor, "users.manage"):
        raise Forbidden("forbidden", "This person may not open IntraForge Pay wallets in this shop.")
    app_id, external_id = str(uuid.UUID(str(source["app_id"]))), str(source["external_id"])
    existing = conn.execute("SELECT * FROM users WHERE source_app_id = %s AND source_external_id = %s",
                            (app_id, external_id)).fetchone()
    if existing is not None:
        return existing, None
    display_name = display_name.strip()[:80]
    base = username_base(username)
    password = first_password()
    for n in range(1, 50):
        candidate = base if n == 1 else f"{base}-{n}"
        try:
            with conn.transaction():
                user = conn.execute(
                    """INSERT INTO users (username, display_name, role, password_hash, created_by, source_app_id,
                                          source_external_id, must_change_password)
                       VALUES (%s, %s, 'customer', %s, %s, %s, %s, true) RETURNING *""",
                    (candidate, display_name, hash_password(password), actor["id"], app_id, external_id),
                ).fetchone()
        except psycopg.errors.UniqueViolation:
            again = conn.execute("SELECT * FROM users WHERE source_app_id = %s AND source_external_id = %s",
                                 (app_id, external_id)).fetchone()
            if again is not None:  # the same record, opened by a request that ran at the same time
                return again, None
            continue  # the username is taken: try the next number
        except psycopg.errors.CheckViolation as exc:
            raise DomainError("invalid_user", "The customer's name must be 1-80 characters.") from exc
        ledger.open_customer_account(conn, user)
        audit.record(conn, actor=actor, action="user.create", subject_type="user", subject_id=user["id"],
                     detail={"username": candidate, "role": "customer", "source_app_id": app_id,
                             "source_external_id": external_id, "source_app_name": source.get("app_name", "")})
        return user, password
    raise Conflict("username_taken", "No free username close to that name; choose another.")


def change_own_password(conn, *, user: dict, current_password: str, new_password: str) -> dict:
    """A customer replaces their password (and so the one-time first password). The old sessions end."""
    if user["role"] != "customer":
        raise DomainError("staff_in_intraforge", "Shop staff sign in through IntraForge.")
    stored = conn.execute("SELECT password_hash FROM users WHERE id = %s", (user["id"],)).fetchone()["password_hash"]
    if not verify_password(current_password, stored):
        audit.record(conn, actor=user, action="user.password_change", result="denied", subject_type="user",
                     subject_id=user["id"], detail={"reason": "wrong_current_password"})
        conn.commit()
        raise DomainError("wrong_password", "The current password is wrong.", status=403)
    problem = validate_new_password(new_password)
    if problem:
        raise DomainError("weak_password", problem)
    if new_password == current_password:
        raise DomainError("same_password", "Choose a password different from the current one.")
    row = conn.execute(
        "UPDATE users SET password_hash = %s, must_change_password = false WHERE id = %s RETURNING *",
        (hash_password(new_password), user["id"]),
    ).fetchone()
    audit.record(conn, actor=user, action="user.password_change", subject_type="user", subject_id=user["id"])
    return row


def deactivate_user(conn, *, actor: dict, user_id) -> dict:
    if not has_capability(actor, "users.manage"):
        raise Forbidden("forbidden", "You are not allowed to manage users.")
    user = get_user(conn, user_id)
    if user["role"] != "customer":
        raise DomainError("staff_in_intraforge", "Shop staff are managed in IntraForge (Members and roles).")
    if user["id"] == actor["id"]:
        raise DomainError("cannot_deactivate_self", "You cannot deactivate your own account.")
    conn.execute("UPDATE users SET active = false WHERE id = %s", (user_id,))
    conn.execute("UPDATE sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL", (user_id,))
    audit.record(conn, actor=actor, action="user.deactivate", subject_type="user", subject_id=user_id,
                 detail={"username": user["username"]})
    return get_user(conn, user_id)


def staff_username(intraforge_user_id: str) -> str:
    return "if-" + uuid.UUID(str(intraforge_user_id)).hex


def sync_staff(conn, *, intraforge_user_id: str, display_name: str, capabilities: frozenset[str],
               merchant: dict) -> dict:
    """The row for an IntraForge person who just presented a live Pay session, with this request's capabilities
    attached. Created on first use; the name follows IntraForge."""
    name = (display_name or "Staff").strip()[:200] or "Staff"
    row = conn.execute(
        """INSERT INTO users (username, display_name, role, intraforge_user_id, password_hash)
           VALUES (%s, %s, 'staff', %s, NULL)
           ON CONFLICT (intraforge_user_id) DO UPDATE SET display_name = EXCLUDED.display_name, active = true
              WHERE users.display_name IS DISTINCT FROM EXCLUDED.display_name OR NOT users.active
           RETURNING *, (xmax = 0) AS inserted""",
        (staff_username(intraforge_user_id), name, intraforge_user_id),
    ).fetchone()
    if row is None:  # already known and unchanged: no write on every request
        row = conn.execute("SELECT *, false AS inserted FROM users WHERE intraforge_user_id = %s",
                           (intraforge_user_id,)).fetchone()
    conn.execute("INSERT INTO merchant_staff (merchant_id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                 (merchant["id"], row["id"]))
    if row.pop("inserted"):
        audit.record(conn, actor=None, action="staff.first_open", subject_type="user", subject_id=row["id"],
                     detail={"intraforge_user_id": str(intraforge_user_id), "name": name})
    row["capabilities"] = capabilities
    return row


def ensure_merchant(conn, *, name: str, intraforge_app_id: str) -> dict:
    """The shop this server is: the IntraForge Pay instance. Created on first start; renamed when the shop is
    renamed in IntraForge."""
    name = name.strip()[:200]
    merchant = the_merchant(conn)
    if merchant is None:
        merchant = create_merchant(conn, name=name)
    if merchant["name"] != name or str(merchant["intraforge_application_id"] or "") != str(intraforge_app_id):
        merchant = conn.execute(
            "UPDATE merchants SET name = %s, intraforge_application_id = %s WHERE id = %s RETURNING *",
            (name, intraforge_app_id, merchant["id"]),
        ).fetchone()
    return merchant


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
    out = {
        "id": str(user["id"]),
        "username": user["username"],
        "display_name": user["display_name"],
        "role": user["role"],
        "active": user["active"],
    }
    if user["role"] == "customer":
        out["must_change_password"] = bool(user.get("must_change_password"))
    if "capabilities" in user:
        out["capabilities"] = sorted(user["capabilities"])
    return out
