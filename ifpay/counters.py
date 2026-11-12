"""A shop's counters (tills): named by the Pay Manager; a cashier picks theirs, and each payment request records it."""

from __future__ import annotations

from . import audit, directory
from .errors import Conflict, DomainError, Forbidden, NotFound
from .security import has_capability


def list_counters(conn, *, actor: dict, include_inactive: bool = False) -> list[dict]:
    if not has_capability(actor, "shop.read"):
        raise Forbidden("forbidden", "Only shop staff can see the counters.")
    merchant = directory.merchant_for_staff(conn, actor)
    return conn.execute(
        "SELECT * FROM counters WHERE merchant_id = %s" + ("" if include_inactive else " AND active") + " ORDER BY name",
        (merchant["id"],),
    ).fetchall()


def create_counter(conn, *, actor: dict, name: str) -> dict:
    if not has_capability(actor, "users.manage"):
        raise Forbidden("forbidden", "Only the Pay Manager can add counters.")
    name = " ".join((name or "").split())
    if not 1 <= len(name) <= 40:
        raise DomainError("bad_name", "A counter's name is 1 to 40 characters.")
    merchant = directory.merchant_for_staff(conn, actor)
    if conn.execute("SELECT 1 FROM counters WHERE merchant_id = %s AND lower(name) = lower(%s)",
                    (merchant["id"], name)).fetchone():
        raise Conflict("name_taken", "There is already a counter with this name.")
    row = conn.execute("INSERT INTO counters (merchant_id, name) VALUES (%s, %s) RETURNING *",
                       (merchant["id"], name)).fetchone()
    audit.record(conn, actor=actor, action="counter.create", subject_type="counter", subject_id=row["id"],
                 detail={"name": name})
    return row


def set_active(conn, *, actor: dict, counter_id, active: bool) -> dict:
    if not has_capability(actor, "users.manage"):
        raise Forbidden("forbidden", "Only the Pay Manager can change counters.")
    merchant = directory.merchant_for_staff(conn, actor)
    row = conn.execute("UPDATE counters SET active = %s WHERE id = %s AND merchant_id = %s RETURNING *",
                       (active, counter_id, merchant["id"])).fetchone()
    if row is None:
        raise NotFound("unknown_counter", "No such counter in this shop.")
    audit.record(conn, actor=actor, action="counter.enable" if active else "counter.disable", subject_type="counter",
                 subject_id=row["id"], detail={"name": row["name"]})
    return row


def usable(conn, merchant_id, counter_id) -> dict | None:
    """The shop's active counter with this id, or None when no counter was named; a wrong one is refused."""
    if counter_id in (None, ""):
        return None
    row = conn.execute("SELECT * FROM counters WHERE id = %s AND merchant_id = %s", (str(counter_id), merchant_id)).fetchone()
    if row is None or not row["active"]:
        raise DomainError("unknown_counter", "Choose one of this shop's counters.")
    return row
