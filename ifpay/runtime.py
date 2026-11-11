"""Pay's life inside IntraForge: wait to be installed, open the provisioned schema, keep in touch.

On start the server asks IntraForge which instance it is (``GET self``). Until an owner installs IntraForge Pay into a
shop from IntraForge's Apps page the answer is "not installed" and every page says so. Once installed, Pay connects to
the schema IntraForge provisioned (as this deployment's own database role), applies its migrations, makes the shop
match the instance (name, id), and from then on sends a heartbeat every minute and relays its audit events into
IntraForge's audit log every few seconds.
"""

from __future__ import annotations

import logging
import threading
import time

from psycopg.types.json import Jsonb

from . import audit, db, directory, platform
from .config import VERSION, get_settings

log = logging.getLogger("ifpay.runtime")

HEARTBEAT_SECONDS = 60
RELAY_SECONDS = 5
RETRY_SECONDS = 10
RELAY_BATCH = 200

_lock = threading.Lock()
_instance: platform.Instance | None = None
_client: platform.PlatformClient | None = None
_stop = threading.Event()
_thread: threading.Thread | None = None


def instance() -> platform.Instance | None:
    return _instance


def client() -> platform.PlatformClient:
    global _client
    with _lock:
        if _client is None:
            _client = platform.client()
        return _client


def reset() -> None:
    """Forget the instance and the client (tests)."""
    global _instance, _client
    with _lock:
        if _client is not None:
            _client.close()
        _client = None
        _instance = None


# --- coming up -----------------------------------------------------------------------------------------------------


def bring_up() -> platform.Instance:
    """Ask IntraForge who we are, open the schema, migrate, and make the shop match the instance."""
    global _instance
    found = client().instance()
    settings = get_settings()
    url = settings.database_url or db.conninfo_for(host=settings.db_host, port=settings.db_port,
                                                   dbname=settings.db_name, role=found.db_role,
                                                   password=settings.db_password, schema=found.schema)
    applied = db.migrate(url)
    if applied:
        log.info("applied migrations: %s", ", ".join(applied))
    db.open_pool(url)
    with db.connection() as conn:
        directory.ensure_merchant(conn, name=found.shop_name, intraforge_app_id=found.app_id)
        record_status(conn, ok=found.enabled, detail=describe(found))
    _instance = found
    return found


def describe(found: platform.Instance) -> dict:
    return {"ok": found.enabled, "app_id": found.app_id, "shop": found.shop_name,
            "organization": found.organization_name, "deployment": found.deployment, "enabled": found.enabled}


def record_status(conn, *, ok: bool, detail: dict) -> None:
    conn.execute(
        """INSERT INTO integration_status (id, ok, checked_at, detail) VALUES (1, %s, now(), %s)
           ON CONFLICT (id) DO UPDATE SET ok = EXCLUDED.ok, checked_at = now(), detail = EXCLUDED.detail""",
        (ok, Jsonb(detail)),
    )


def refresh(*, actor: dict | None = None) -> dict:
    """Re-read the instance from IntraForge (rename, disable) and record whether the shop is verified."""
    global _instance
    try:
        found = client().instance()
        result = describe(found)
        _instance = found
        with db.connection() as conn:
            directory.ensure_merchant(conn, name=found.shop_name, intraforge_app_id=found.app_id)
    except platform.IntegrationError as exc:
        result = {"ok": False, "code": exc.code, "message": exc.message}
    with db.connection() as conn:
        before = conn.execute("SELECT ok FROM integration_status WHERE id = 1").fetchone()
        record_status(conn, ok=bool(result["ok"]), detail=result)
        # The minute-by-minute check is recorded only when asked for, or when the answer changes.
        if actor is not None or before is None or before["ok"] != bool(result["ok"]):
            audit.record(conn, actor=actor, action="intraforge.verify",
                         result="success" if result["ok"] else "error", subject_type="intraforge_instance",
                         detail=result)
    return result


# --- audit relay ---------------------------------------------------------------------------------------------------


def _event_id(app_id: str, event_id: int) -> str:
    # IntraForge keeps one receipt per event id across every service application: make ours globally unique.
    return f"ifpay-{app_id.replace('-', '')}-{event_id}"


def relay_audit_once() -> int:
    """Send up to RELAY_BATCH not-yet-relayed audit events to IntraForge; returns how many IntraForge accepted."""
    found = _instance
    if found is None or not db.is_open():
        return 0
    with db.connection() as conn:
        rows = conn.execute(
            """SELECT a.*, u.intraforge_user_id, u.display_name AS actor_name, u.role AS actor_role
               FROM audit_events a
               LEFT JOIN audit_relay r ON r.event_id = a.id
               LEFT JOIN users u ON u.id = a.actor_id
               WHERE r.event_id IS NULL ORDER BY a.id LIMIT %s""",
            (RELAY_BATCH,),
        ).fetchall()
    if not rows:
        return 0
    events, by_id = [], {}
    for r in rows:
        eid = _event_id(found.app_id, r["id"])
        by_id[eid] = r["id"]
        label = r["actor_name"] or r["actor_username"] or "IntraForge Pay"
        if r["actor_role"] == "customer":
            label = f"customer {r['actor_username']} ({r['actor_name']})"
        events.append({
            "id": eid,
            "action": r["action"][:60],
            "entityType": (r["subject_type"] or "pay")[:50],
            "entityId": (r["subject_id"] or "")[:64],
            "actorId": str(r["intraforge_user_id"]) if r["intraforge_user_id"] else None,
            "actorLabel": label,
            # success / denied / error: IntraForge records it as the event's result, so a refusal reads as one there.
            "result": r["result"],
            "summary": f"{r['action']} ({r['result']})",
            "metadata": {"result": r["result"], **(r["detail"] or {})},
            "at": r["occurred_at"].isoformat(),
        })
    accepted = client().relay_audit(events)
    with db.connection() as conn:
        for eid in accepted:
            if eid in by_id:
                conn.execute("INSERT INTO audit_relay (event_id) VALUES (%s) ON CONFLICT DO NOTHING", (by_id[eid],))
    return len(accepted)


# --- the background thread -----------------------------------------------------------------------------------------


def _run() -> None:
    while not _stop.is_set():
        try:
            bring_up()
            break
        except platform.IntegrationError as exc:
            log.info("waiting for IntraForge: %s", exc.message)
        except Exception:  # noqa: BLE001 -- keep trying; the cause is logged
            log.exception("could not start")
        _stop.wait(RETRY_SECONDS)
    last_beat = 0.0
    while not _stop.is_set():
        now = time.monotonic()
        try:
            if now - last_beat >= HEARTBEAT_SECONDS:
                client().heartbeat(VERSION)
                refresh()
                last_beat = now
            relay_audit_once()
        except platform.IntegrationError as exc:
            log.warning("IntraForge: %s", exc.message)
        except Exception:  # noqa: BLE001
            log.exception("background step failed")
        _stop.wait(RELAY_SECONDS)


def start() -> None:
    global _thread
    _stop.clear()
    _thread = threading.Thread(target=_run, name="ifpay-intraforge", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=5)
    try:
        relay_audit_once()
    except Exception:  # noqa: BLE001 -- best effort on shutdown; the next start relays the rest
        pass
