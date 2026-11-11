"""IntraForge's internal service-application API (IntraForge ADR-0018): Pay runs inside IntraForge as a service app.

Pay is installed into one shop (an IntraForge organization) from IntraForge's Apps page. IntraForge then owns:

  * identity -- "Open" in IntraForge mints an application session (HttpOnly cookie ``ifapp_<slot>`` on Pay's path);
    Pay never sees a staff password and resolves the cookie on every request through ``sessions/introspect``;
  * authority -- the person's ``ifpay.*`` capabilities in that shop (Pay Cashier, Pay Supervisor, ... roles);
  * storage -- Pay's ledger lives in a schema IntraForge provisioned in its tenant PostgreSQL (owned by this
    deployment's own login role), and receipts go to the instance's own bucket through ``files``;
  * audit -- Pay's audit events are relayed into IntraForge's audit log (``audit-events``, idempotent by event id).

Every call carries this deployment's service token (``X-IntraForge-Service-Token``) over IntraForge's internal Docker
network; the proxy refuses ``/api/v1/internal/`` from outside. The token names one deployment slot, so Pay can only
ever reach its own instance. This module stores documents and asks who is signed in; it never authorizes a payment.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import httpx

from .config import get_settings

INTERNAL = "/api/v1/internal/service-apps"
TOKEN_HEADER = "X-IntraForge-Service-Token"


class IntegrationError(Exception):
    """`code` is one of: not_configured, not_installed, disabled, rejected, unavailable, upload_failed."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Instance:
    """What IntraForge says this Pay deployment is (``GET self``)."""

    deployment: str
    launch_path: str
    app_id: str
    display_name: str
    shop_name: str
    organization_id: str
    organization_name: str
    schema: str
    db_role: str
    enabled: bool

    @property
    def cookie_name(self) -> str:
        return "ifapp_" + self.deployment.replace("-", "_")


@dataclass(frozen=True)
class StaffSession:
    """A live IntraForge application session (``sessions/introspect``)."""

    user_id: str
    email: str
    display_name: str
    capabilities: frozenset[str]
    csrf_token: str


# Tests replace this with an in-process fake IntraForge (httpx.MockTransport); None means the real internal network.
DEFAULT_TRANSPORT: httpx.BaseTransport | None = None


class PlatformClient:
    def __init__(self, base_url: str, token: str, *, transport: httpx.BaseTransport | None = None,
                 timeout: float = 10.0):
        kwargs: dict = {"base_url": base_url.rstrip("/"), "timeout": timeout,
                        "headers": {TOKEN_HEADER: token, "Accept": "application/json"}}
        if transport is not None:
            kwargs["transport"] = transport
        self._http = httpx.Client(**kwargs)

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _request(self, method: str, path: str, *, ok=(200, 201), **kwargs) -> httpx.Response:
        try:
            response = self._http.request(method, INTERNAL + path, **kwargs)
        except httpx.HTTPError as exc:
            raise IntegrationError("unavailable", f"IntraForge is not reachable ({type(exc).__name__}).") from exc
        if response.status_code in ok:
            return response
        if response.status_code in (401, 403) and path != "/sessions/introspect/":
            raise IntegrationError("rejected", "IntraForge refused this Pay deployment's service token.")
        if response.status_code == 404:
            raise IntegrationError("not_installed", "IntraForge Pay is not installed in a shop yet.")
        raise IntegrationError("upload_failed" if method == "POST" else "unavailable",
                               f"IntraForge answered HTTP {response.status_code}.")

    # --- the instance --------------------------------------------------------------------------------------------

    def instance(self) -> Instance:
        body = self._request("GET", "/self/").json()
        if not body.get("installed"):
            raise IntegrationError("not_installed", "IntraForge Pay is not installed in a shop yet. Install it from "
                                                    "IntraForge > Apps.")
        if body.get("app_type") != "intraforge_pay":
            raise IntegrationError("rejected", "This service token belongs to a different application type.")
        app = body["app"]
        return Instance(
            deployment=body["deployment"], launch_path=body["launch_path"], app_id=app["id"],
            display_name=app["display_name"], shop_name=(body.get("config") or {}).get("shopName")
            or app["display_name"], organization_id=app["organization"]["id"],
            organization_name=app["organization"]["name"], schema=body["database"]["schema"],
            db_role=body["database"]["role"], enabled=bool(body.get("enabled", True)))

    def heartbeat(self, version: str) -> bool:
        return bool(self._request("POST", "/heartbeat/", json={"version": version}).json().get("enabled", True))

    # --- identity ------------------------------------------------------------------------------------------------

    def introspect(self, token: str) -> StaffSession | None:
        response = self._request("POST", "/sessions/introspect/", ok=(200, 401), json={"token": token})
        body = response.json()
        if response.status_code != 200 or not body.get("active"):
            return None
        user = body["user"]
        return StaffSession(user_id=str(user["id"]), email=str(user.get("email", "")),
                            display_name=str(user.get("displayName") or user.get("email") or "Staff"),
                            capabilities=frozenset(body.get("capabilities") or ()),
                            csrf_token=str(body.get("csrfToken", "")))

    def sign_out(self, token: str) -> None:
        """Ends the IntraForge login this session came from too (a shared counter PC needs the next person's own
        sign-in), exactly as IntraForge's own Log out does."""
        self._request("POST", "/sessions/revoke/", json={"token": token, "sign_out": True})

    # --- files and audit -----------------------------------------------------------------------------------------

    def store_file(self, *, filename: str, content: bytes, mime_type: str, kind: str, actor_id: str | None) -> dict:
        data = {"kind": kind}
        if actor_id:
            data["actorId"] = actor_id
        return self._request("POST", "/files/", files={"file": (filename, content, mime_type)}, data=data).json()

    def relay_audit(self, events: list[dict]) -> list[str]:
        return list(self._request("POST", "/audit-events/", json={"events": events}).json().get("accepted", []))


def client(*, transport: httpx.BaseTransport | None = None) -> PlatformClient:
    settings = get_settings()
    if not settings.intraforge_url or not settings.service_token:
        raise IntegrationError("not_configured", "IFPAY_INTRAFORGE_URL and the service token are not configured.")
    return PlatformClient(settings.intraforge_url, settings.service_token, transport=transport or DEFAULT_TRANSPORT)


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
