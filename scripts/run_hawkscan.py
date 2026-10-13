"""Run the HawkScan (StackHawk DAST) security scan against the ISOLATED scan instance of the shop server.

  python scripts/run_hawkscan.py validate     # check config, API spec and sign-in only
  python scripts/run_hawkscan.py scan         # full scan (several minutes); results also at app.stackhawk.com

Needs: the hawk CLI (default C:\\Users\\Hp\\tools\\hawk\\hawk.exe, or $HAWK_EXE), the StackHawk API key in
secrets/hawk_api_key, and the scan instance running (docs/MANUAL.md section 13). Signs in as a test customer of the
scan instance and hands the session cookie + CSRF token to hawk through environment variables; nothing secret is
written to stackhawk.yml or printed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
HAWK = os.environ.get("HAWK_EXE", r"C:\Users\Hp\tools\hawk\hawk.exe")
SCAN_HOST = os.environ.get("IFPAY_SCAN_HOST", "https://localhost:8644")
SCAN_SECRETS = ROOT / "secrets" / "scan"
SCAN_USER = os.environ.get("IFPAY_SCAN_USER", "customer4")


def scan_session() -> tuple[str, str]:
    password = json.loads((SCAN_SECRETS / "test-accounts.json").read_text(encoding="utf-8"))[SCAN_USER]["password"]
    with httpx.Client(base_url=SCAN_HOST, verify=str(SCAN_SECRETS / "tls" / "ca.crt"), timeout=20) as client:
        r = client.post("/api/auth/login", json={"username": SCAN_USER, "password": password})
        r.raise_for_status()
        return r.cookies["ifpay_session"], r.json()["csrf_token"]


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "validate"
    if mode not in ("validate", "scan"):
        sys.exit(__doc__)
    subprocess.run([sys.executable, str(ROOT / "scripts" / "export_openapi.py")], check=True, cwd=ROOT)
    session, csrf = scan_session()
    key = (ROOT / "secrets" / "hawk_api_key").read_text(encoding="utf-8").strip()
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    env = {**os.environ, "API_KEY": key, "HAWK_API_KEY": key, "APP_HOST": SCAN_HOST,
           "IFPAY_SCAN_SESSION": session, "IFPAY_SCAN_CSRF": csrf,
           "HAWK_AGENT": "claude-code", "COMMIT_SHA": commit or "none", "BRANCH_NAME": "none"}
    steps = [["validate", "config", "stackhawk.yml"], ["validate", "api", "stackhawk.yml"],
             ["validate", "auth", "stackhawk.yml"]]
    if mode == "scan":
        steps.append(["scan", "stackhawk.yml"])
    for step in steps:
        print(f"StackHawk | hawk {' '.join(step)}", flush=True)
        code = subprocess.run([HAWK, *step], cwd=ROOT, env=env).returncode
        if code != 0:
            print(f"StackHawk | hawk {' '.join(step)} exited {code}", flush=True)
            return code
    return 0


if __name__ == "__main__":
    sys.exit(main())
