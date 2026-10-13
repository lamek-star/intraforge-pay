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
            actor["username"] if actor else "",
            action,
            result,
            subject_type,
            str(subject_id) if subject_id else "",
            Jsonb(detail or {}),
        ),
    )
