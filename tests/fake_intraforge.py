"""An in-process stand-in for the IntraForge endpoints the pilot uses, with IntraForge's verified semantics:

* bearer `pdc_sk_<id>.<secret>`; unknown/revoked -> 403 with error code "authentication_failed" (the same
  response either way; IntraForge answers 403, not 401 -- found by the live suite);
* the credential authenticates as service-account+<application id>@service.internal (GET auth/me/);
* bucket access needs a per-bucket grant: storage.read for GET folders/files, storage.write for POST files; no grant
  -> 403 "permission_denied";
* upload is multipart field `file` plus `display_filename`; the response carries the new file id.

The live suite (tests/test_live_intraforge.py) checks the same behaviours against a real IntraForge.
"""

from __future__ import annotations

import re
import secrets
import uuid

import httpx


class FakeIntraForge:
    def __init__(self):
        self.application_id = str(uuid.uuid4())
        self.bucket_id = str(uuid.uuid4())
        self.other_bucket_id = str(uuid.uuid4())
        self.credentials: dict[str, dict] = {}
        self.grants: set[tuple[str, str]] = {("storage.read", self.bucket_id), ("storage.write", self.bucket_id)}
        self.files: dict[str, dict] = {}
        self.audit: list[dict] = []
        self.down = False

    def issue(self) -> str:
        cid, secret = uuid.uuid4().hex, secrets.token_urlsafe(16)
        self.credentials[cid] = {"secret": secret, "revoked": False}
        return f"pdc_sk_{cid}.{secret}"

    def revoke(self, token: str) -> None:
        self.credentials[token[len("pdc_sk_"):].split(".")[0]]["revoked"] = True

    def config(self, ca_bundle=None) -> dict:
        return {"base_url": "https://intraforge.test", "organization_id": str(uuid.uuid4()),
                "application_id": self.application_id, "application_name": "Pilot Shop",
                "project_id": str(uuid.uuid4()), "bucket_id": self.bucket_id, "bucket_name": "Pilot Shop Receipts"}

    def _authenticated(self, request: httpx.Request) -> bool:
        header = request.headers.get("authorization", "")
        match = re.match(r"^Bearer pdc_sk_([0-9a-f]{32})\.(.+)$", header)
        if not match:
            return False
        cred = self.credentials.get(match.group(1))
        return bool(cred and not cred["revoked"] and cred["secret"] == match.group(2))

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("intraforge down", request=request)
        if not self._authenticated(request):
            return httpx.Response(403, json={"error": {"code": "authentication_failed",
                                                        "message": {"detail": "Invalid or expired credential."}}})
        path = request.url.path
        if path == "/api/v1/auth/me/" and request.method == "GET":
            return httpx.Response(200, json={"email": f"service-account+{self.application_id}@service.internal"})
        match = re.match(r"^/api/v1/buckets/([0-9a-f-]{36})/(folders|files)/$", path)
        if match:
            bucket, what = match.groups()
            need = "storage.write" if request.method == "POST" else "storage.read"
            if (need, bucket) not in self.grants:
                return httpx.Response(403, json={"error": {"code": "permission_denied", "message": {
                    "detail": "You do not have permission to perform this action."}}})
            if request.method == "GET":
                return httpx.Response(200, json=[f for f in self.files.values() if f["bucket"] == bucket]
                                      if what == "files" else [])
            if what == "files" and request.method == "POST":
                body = request.read()
                name = re.search(rb'name="display_filename"\r\n\r\n([^\r]*)', body)
                file_id = str(uuid.uuid4())
                self.files[file_id] = {"id": file_id, "bucket": bucket,
                                       "display_filename": name.group(1).decode() if name else "", "size": len(body)}
                self.audit.append({"action": "storage.file.upload", "file": file_id,
                                   "display_filename": self.files[file_id]["display_filename"]})
                return httpx.Response(201, json=self.files[file_id])
        return httpx.Response(404)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
