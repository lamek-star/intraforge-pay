"""Integration with IntraForge through a developer ApplicationCredential (bearer token).

Endpoints used -- all verified against the IntraForge source (apps/backend) and a live IntraForge 1.2.0 instance:

  GET  /api/v1/auth/me/                    who the credential authenticates as: the Application's service account,
                                           whose e-mail is service-account+<application id>@service.internal
  GET  /api/v1/buckets/{bucket}/folders/   needs storage.read on that bucket (ResourceGrant storage.bucket)
  POST /api/v1/buckets/{bucket}/files/     multipart upload; needs storage.write on that bucket
  GET  /api/v1/files/{file}/               needs storage.read on the file's bucket

The credential (pdc_sk_...) stays on this server: it is read from a file each time it is used (so a rotated credential
takes effect without a restart), and it is never logged, stored in the database, or sent to a browser. The non-secret
configuration (URLs and ids) lives in the database, entered through the configuration wizard.

This module stores documents. It never authorizes a payment.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx
from psycopg.types.json import Jsonb

from .config import get_settings

CONFIG_FIELDS = ("base_url", "organization_id", "application_id", "application_name", "project_id", "bucket_id",
                 "bucket_name")
_UUID_FIELDS = ("organization_id", "application_id", "project_id", "bucket_id")
_SECRET_LOOKING = re.compile(r"pdc_sk_|secret|password|token|credential", re.IGNORECASE)


class IntegrationError(Exception):
    """`code` is one of: not_configured, credential_missing, credential_rejected, not_authorized, not_found,
    wrong_application, unavailable, upload_failed."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class IntraForgeConfig:
    base_url: str
    organization_id: str
    application_id: str
    application_name: str
    project_id: str
    bucket_id: str
    bucket_name: str
    ca_bundle: str | None = None

    @classmethod
    def validate(cls, data: dict) -> "IntraForgeConfig":
        """Accepts only the known non-secret fields; refuses anything that looks like a secret."""
        if not isinstance(data, dict):
            raise ValueError("The configuration must be a JSON object.")
        for key, value in data.items():
            if _SECRET_LOOKING.search(str(key)) or _SECRET_LOOKING.search(str(value)):
                raise ValueError("This configuration contains a secret. Secrets are never uploaded through the "
                                 "browser: the credential file stays on the shop server (see the manual).")
        unknown = set(data) - set(CONFIG_FIELDS) - {"ca_bundle", "format"}
        if unknown:
            raise ValueError(f"Unknown configuration field(s): {', '.join(sorted(unknown))}.")
        missing = [f for f in CONFIG_FIELDS if not str(data.get(f, "")).strip()]
        if missing:
            raise ValueError(f"Missing field(s): {', '.join(missing)}.")
        base_url = str(data["base_url"]).strip().rstrip("/")
        if not re.match(r"^https://[A-Za-z0-9.\-]+(:\d{1,5})?$", base_url):
            raise ValueError("base_url must be an https:// origin such as https://localhost:9443 (no path).")
        for f in _UUID_FIELDS:
            try:
                uuid.UUID(str(data[f]))
            except ValueError as exc:
                raise ValueError(f"{f} must be a UUID.") from exc
        ca_bundle = data.get("ca_bundle") or None
        if ca_bundle is not None:
            ca_bundle = str(ca_bundle)
            if not Path(ca_bundle).is_file():
                raise ValueError(f"ca_bundle file not found on the shop server: {ca_bundle}")
        return cls(base_url=base_url, organization_id=str(data["organization_id"]),
                   application_id=str(data["application_id"]), application_name=str(data["application_name"]).strip(),
                   project_id=str(data["project_id"]), bucket_id=str(data["bucket_id"]),
                   bucket_name=str(data["bucket_name"]).strip(), ca_bundle=ca_bundle)

    def as_dict(self) -> dict:
        return {f: getattr(self, f) for f in (*CONFIG_FIELDS, "ca_bundle")}


def load_config(conn) -> IntraForgeConfig | None:
    row = conn.execute("SELECT config FROM integration_settings WHERE id = 1").fetchone()
    return IntraForgeConfig.validate(row["config"]) if row else None


def save_config(conn, *, config: IntraForgeConfig, actor_id) -> None:
    conn.execute(
        """INSERT INTO integration_settings (id, config, updated_by, updated_at) VALUES (1, %s, %s, now())
           ON CONFLICT (id) DO UPDATE SET config = EXCLUDED.config, updated_by = EXCLUDED.updated_by, updated_at = now()""",
        (Jsonb(config.as_dict()), actor_id),
    )
    conn.execute("UPDATE merchants SET intraforge_application_id = %s", (config.application_id,))
    conn.execute("DELETE FROM integration_status")


def read_credential() -> str:
    path = get_settings().intraforge_credential_file
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise IntegrationError("credential_missing", "The IntraForge credential file is not present on the shop "
                                                     "server.") from exc
    if not token.startswith("pdc_sk_"):
        raise IntegrationError("credential_missing", "The IntraForge credential file does not hold a credential.")
    return token


class IntraForgeClient:
    def __init__(self, config: IntraForgeConfig, token: str, *, transport: httpx.BaseTransport | None = None,
                 timeout: float = 10.0):
        self.config = config
        kwargs: dict = {"base_url": config.base_url, "timeout": timeout,
                        "headers": {"Authorization": f"Bearer {token}", "Accept": "application/json"}}
        if transport is not None:
            kwargs["transport"] = transport
        else:
            kwargs["verify"] = config.ca_bundle or True
        self._http = httpx.Client(**kwargs)

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise IntegrationError("unavailable", f"IntraForge is not reachable ({type(exc).__name__}).") from exc
        # IntraForge answers a revoked, expired or unknown bearer token with 403 (not 401: DRF takes the
        # WWW-Authenticate header from its first authenticator, SessionAuthentication, which has none) and the
        # error code "authentication_failed"; a missing grant is 403 "permission_denied" or an empty 403.
        if response.status_code == 401 or (response.status_code == 403 and _error_code(response) in
                                           ("authentication_failed", "not_authenticated")):
            raise IntegrationError("credential_rejected", "IntraForge rejected the application credential "
                                                          "(revoked, expired or wrong).")
        if response.status_code == 403:
            raise IntegrationError("not_authorized", "The Pilot Shop application is not authorized for this "
                                                     "IntraForge resource.")
        if response.status_code == 404:
            raise IntegrationError("not_found", "IntraForge resource not found (or not visible to this application).")
        if response.status_code >= 400:
            raise IntegrationError("upload_failed" if method == "POST" else "unavailable",
                                   f"IntraForge answered HTTP {response.status_code}.")
        return response

    def whoami(self) -> dict:
        return self._request("GET", "/api/v1/auth/me/").json()

    def verify(self) -> dict:
        """The credential is valid, belongs to the configured Application, and can read the receipts bucket."""
        me = self.whoami()
        expected = f"service-account+{self.config.application_id}@service.internal"
        if str(me.get("email", "")).lower() != expected.lower():
            raise IntegrationError("wrong_application", "The credential belongs to a different IntraForge "
                                                        "application than the one configured.")
        self._request("GET", f"/api/v1/buckets/{self.config.bucket_id}/folders/")
        return {"application_id": self.config.application_id, "bucket_id": self.config.bucket_id}

    def upload(self, *, filename: str, content: bytes, mime_type: str, bucket_id: str | None = None) -> dict:
        bucket = bucket_id or self.config.bucket_id
        response = self._request(
            "POST",
            f"/api/v1/buckets/{bucket}/files/",
            files={"file": (filename, content, mime_type)},
            data={"display_filename": filename},
        )
        return response.json()

    def get_file(self, file_id: str) -> dict:
        return self._request("GET", f"/api/v1/files/{file_id}/").json()

    def list_files(self, bucket_id: str | None = None) -> list[dict]:
        return self._request("GET", f"/api/v1/buckets/{bucket_id or self.config.bucket_id}/files/").json()


def _error_code(response: httpx.Response) -> str:
    """IntraForge's error envelope: {"error": {"code": "...", "message": {...}, "request_id": "..."}}."""
    try:
        body = response.json()
    except ValueError:
        return ""
    error = body.get("error") if isinstance(body, dict) else None
    return str(error.get("code", "")) if isinstance(error, dict) else ""


# Tests replace this with an in-process fake IntraForge (httpx.MockTransport); None means real HTTPS.
DEFAULT_TRANSPORT: httpx.BaseTransport | None = None


def client_from(conn, *, transport: httpx.BaseTransport | None = None) -> IntraForgeClient:
    config = load_config(conn)
    if config is None:
        raise IntegrationError("not_configured", "The IntraForge connection has not been configured yet.")
    return IntraForgeClient(config, read_credential(), transport=transport or DEFAULT_TRANSPORT)


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
