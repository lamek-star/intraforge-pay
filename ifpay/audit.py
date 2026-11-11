"""Append-only audit log (the table rejects UPDATE/DELETE/TRUNCATE)."""

from __future__ import annotations

from psycopg.types.json import Jsonb


def record(conn, *, actor: dict | None, action: str, result: str = "success", subject_type: str = "",
           subject_id: object = "", detail: dict | None = None) -> None:
    conn.execute(
        """INSERT INTO audit_events (actor_id, actor_username, action, result, subject_type, subject_id, detail)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (
            actor["id"] if actor else None,
            _label(actor),
            action,
            result,
            subject_type,
            str(subject_id) if subject_id else "",
            Jsonb(detail or {}),
        ),
    )


def _label(actor: dict | None) -> str:
    """Customers by username; IntraForge staff by their IntraForge e-mail (their Pay username is an internal id)."""
    if not actor:
        return ""
    if actor.get("role") == "staff":
        return str(actor.get("email") or actor.get("display_name") or actor["username"])
    return actor["username"]
