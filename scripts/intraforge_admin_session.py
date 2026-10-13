"""A signed-in IntraForge administrator session (cookie + CSRF), used only by the setup scripts and live tests.

This is the IntraForge administrator acting through IntraForge's own API, exactly as the IntraForge web UI does. The
shop server never uses an administrator session: it only holds the Pilot Shop application credential.
"""

from __future__ import annotations

import httpx


class IntraForgeAdmin:
    def __init__(self, base_url: str, *, ca_bundle: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.http = httpx.Client(base_url=self.base_url, verify=ca_bundle or True, timeout=30.0,
                                 headers={"Referer": self.base_url + "/", "Accept": "application/json"})

    def _csrf(self) -> str:
        token = self.http.cookies.get("csrftoken")
        if not token:
            self.http.get("/api/v1/auth/csrf/").raise_for_status()
            token = self.http.cookies.get("csrftoken")
        return token or ""

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = kwargs.pop("headers", {})
        if method.upper() not in ("GET", "HEAD"):
            headers["X-CSRFToken"] = self._csrf()
        return self.http.request(method, "/api/v1/" + path.lstrip("/"), headers=headers, **kwargs)

    def ok(self, method: str, path: str, **kwargs):
        response = self.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(f"IntraForge {method} {path} -> HTTP {response.status_code}: {response.text[:300]}")
        return response.json() if response.content else None

    def register(self, email: str, password: str, first_name: str = "", last_name: str = "") -> dict:
        return self.ok("POST", "auth/register/", json={"email": email, "password": password,
                                                       "first_name": first_name, "last_name": last_name})

    def login(self, email: str, password: str) -> dict:
        body = self.ok("POST", "auth/login/", json={"email": email, "password": password})
        if isinstance(body, dict) and body.get("mfa_required"):
            raise RuntimeError("This IntraForge account requires MFA; sign in once in the browser and use an account "
                               "without MFA for the scripted pilot setup, or complete the setup in the web UI.")
        self.http.cookies.pop("csrftoken", None)  # Django rotates the CSRF token at login
        return body

    @staticmethod
    def results(body):
        return body["results"] if isinstance(body, dict) and "results" in body else body

    def close(self) -> None:
        self.http.close()
