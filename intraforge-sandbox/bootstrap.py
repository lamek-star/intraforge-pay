"""Sandbox only: create the IntraForge administrator account, organization, workspace and project the pilot uses.

In a real shop these already exist (the IntraForge administrator's own organization); skip this script and pass their
ids to scripts/intraforge_admin_setup.py. Writes .sandbox-admin.json (sandbox credentials; never commit it).
"""

import json
import secrets
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
from intraforge_admin_session import IntraForgeAdmin  # noqa: E402

BASE_URL = "https://localhost:9443"
CA = str(HERE / "caddy-root.crt")
OUT = HERE / ".sandbox-admin.json"

if OUT.exists():
    sys.exit(f"{OUT} already exists; the sandbox is already bootstrapped.")

email = "if-admin@pilot.example"
password = "Sbx-" + secrets.token_urlsafe(18)
admin = IntraForgeAdmin(BASE_URL, ca_bundle=CA)
admin.register(email, password, "IntraForge", "Administrator")
admin.login(email, password)
org = admin.ok("POST", "organizations/", json={"name": "IntraForge Pay Pilot"})
workspace = admin.ok("POST", f"organizations/{org['id']}/workspaces/", json={"name": "Retail pilot"})
project = admin.ok("POST", f"workspaces/{workspace['id']}/projects/", json={"name": "Pilot Shop"})
OUT.write_text(json.dumps({
    "base_url": BASE_URL, "ca_bundle": CA, "email": email, "password": password,
    "organization_id": org["id"], "workspace_id": workspace["id"], "project_id": project["id"],
}, indent=2), encoding="utf-8")
print(f"Sandbox bootstrapped: organization {org['id']}, project {project['id']}. Credentials in {OUT.name}.")
