"""IntraForge administrator setup for the Pilot Shop (manual section 2).

Run ON THE SHOP SERVER by the IntraForge administrator. Through IntraForge's own API it:

  1. registers the application "Pilot Shop"                 POST organizations/{org}/applications/
  2. issues a development application credential           POST applications/{app}/credentials/
     -> written to secrets/intraforge_credential on this server only (shown nowhere, never uploaded)
  3. creates the bucket "Pilot Shop Receipts"              POST projects/{project}/buckets/
  4. grants the application ONLY storage.read + storage.write on that one bucket
                                                           POST applications/{app}/resource-grants/
     (no organization role, no storage.delete/manage, nothing on any other bucket; IntraForge has no payment
      capabilities -- payment authority lives in the shop server's own journal and role checks)
  5. writes config/intraforge.json: the NON-SECRET configuration to load in the pilot's configuration wizard.

Re-running is safe: an existing application/bucket/grant is reused; an existing credential file is kept unless
--rotate-credential is given (then the old credential is revoked in IntraForge).
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from intraforge_admin_session import IntraForgeAdmin  # noqa: E402

APP_NAME = "Pilot Shop"
BUCKET_NAME = "Pilot Shop Receipts"
GRANTS = ("storage.read", "storage.write")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--intraforge", help="IntraForge address, e.g. https://localhost:9443")
    parser.add_argument("--ca-bundle", help="CA certificate that signs IntraForge's HTTPS certificate")
    parser.add_argument("--admin-email")
    parser.add_argument("--organization-id")
    parser.add_argument("--project-id", help="IntraForge project that will hold the receipts bucket")
    parser.add_argument("--sandbox", action="store_true",
                        help="take every value above from intraforge-sandbox/.sandbox-admin.json")
    parser.add_argument("--rotate-credential", action="store_true")
    args = parser.parse_args()

    password = os.environ.get("IFPAY_IF_ADMIN_PASSWORD")
    if args.sandbox:
        sandbox = json.loads((ROOT / "intraforge-sandbox" / ".sandbox-admin.json").read_text(encoding="utf-8"))
        args.intraforge, args.ca_bundle = sandbox["base_url"], sandbox["ca_bundle"]
        args.admin_email, password = sandbox["email"], sandbox["password"]
        args.organization_id, args.project_id = sandbox["organization_id"], sandbox["project_id"]
    for name in ("intraforge", "admin_email", "organization_id", "project_id"):
        if not getattr(args, name):
            parser.error(f"--{name.replace('_', '-')} is required (or use --sandbox)")
    password = password or getpass.getpass(f"IntraForge password for {args.admin_email}: ")

    admin = IntraForgeAdmin(args.intraforge, ca_bundle=args.ca_bundle)
    admin.login(args.admin_email, password)
    org = args.organization_id

    # 1. Application
    apps = admin.results(admin.ok("GET", f"organizations/{org}/applications/"))
    app = next((a for a in apps if a["name"] == APP_NAME), None)
    if app is None:
        app = admin.ok("POST", f"organizations/{org}/applications/",
                       json={"name": APP_NAME, "description": "IntraForge Pay single-shop pilot (simulated value only)"})
        print(f"[1] Registered application '{APP_NAME}': {app['id']}")
    else:
        print(f"[1] Application '{APP_NAME}' already registered: {app['id']}")

    # 2. Credential -- the secret goes straight to a file on this server.
    secret_file = Path(os.environ.get("IFPAY_INTRAFORGE_CREDENTIAL_FILE", ROOT / "secrets" / "intraforge_credential"))
    if secret_file.exists() and not args.rotate_credential:
        print(f"[2] Keeping the existing credential in {secret_file} (use --rotate-credential to replace it)")
    else:
        old_id = None
        if secret_file.exists():
            token = secret_file.read_text(encoding="utf-8").strip()
            old_id = token[len("pdc_sk_"):].split(".")[0] if token.startswith("pdc_sk_") else None
        issued = admin.ok("POST", f"applications/{app['id']}/credentials/")
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        secret_file.write_text(issued["secret"] + "\n", encoding="utf-8")
        try:
            secret_file.chmod(0o600)
        except OSError:
            pass
        print(f"[2] Issued development credential {issued['id']} -> {secret_file} (the secret is not printed)")
        if old_id:
            import uuid

            admin.ok("POST", f"applications/{app['id']}/credentials/{uuid.UUID(old_id)}/revoke/")
            print(f"    Revoked the previous credential {uuid.UUID(old_id)}")

    # 3. Bucket
    buckets = admin.results(admin.ok("GET", f"projects/{args.project_id}/buckets/"))
    bucket = next((b for b in buckets if b["name"] == BUCKET_NAME), None)
    if bucket is None:
        bucket = admin.ok("POST", f"projects/{args.project_id}/buckets/", json={"name": BUCKET_NAME})
        print(f"[3] Created bucket '{BUCKET_NAME}': {bucket['id']}")
    else:
        print(f"[3] Bucket '{BUCKET_NAME}' already exists: {bucket['id']}")

    # 4. Least-privilege grants, scoped to that bucket only.
    existing = admin.results(admin.ok("GET", f"applications/{app['id']}/resource-grants/"))
    have = {(g["permission"], g["resource_type"], str(g["resource_id"])) for g in existing}
    for permission in GRANTS:
        key = (permission, "storage.bucket", str(bucket["id"]))
        if key in have:
            print(f"[4] Grant {permission} on the bucket already present")
            continue
        admin.ok("POST", f"applications/{app['id']}/resource-grants/",
                 json={"permission_code": permission, "resource_type": "storage.bucket", "resource_id": bucket["id"]})
        print(f"[4] Granted {permission} on bucket '{BUCKET_NAME}' only")

    # 5. Non-secret configuration for the wizard.
    config = {
        "format": "ifpay-intraforge-config/1",
        "base_url": args.intraforge.rstrip("/"),
        "organization_id": org,
        "application_id": app["id"],
        "application_name": APP_NAME,
        "project_id": args.project_id,
        "bucket_id": bucket["id"],
        "bucket_name": BUCKET_NAME,
    }
    if args.ca_bundle:
        config["ca_bundle"] = str(Path(args.ca_bundle).resolve())
    out = ROOT / "config" / "intraforge.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    print(f"[5] Wrote the non-secret configuration to {out} -- load it in Admin > IntraForge connection.")
    admin.close()


if __name__ == "__main__":
    main()
