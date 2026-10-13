"""Live tests against a real IntraForge (the sandbox in intraforge-sandbox/): run with `pytest -m live`.

They need: the sandbox running, intraforge-sandbox/.sandbox-admin.json (bootstrap.py), and config/intraforge.json plus
secrets/intraforge_credential (scripts/intraforge_admin_setup.py --sandbox). They create a few extra objects in the
sandbox (a second bucket, one extra credential that is revoked again) and never touch any other IntraForge.
"""

import hashlib
import json
import sys
import uuid

import httpx
import pytest
from conftest import ROOT, Session, balance_of

from ifpay import db, intraforge, receipts
from ifpay.intraforge import IntraForgeClient, IntraForgeConfig, IntegrationError

pytestmark = pytest.mark.live
SANDBOX = ROOT / "intraforge-sandbox" / ".sandbox-admin.json"
CONFIG = ROOT / "config" / "intraforge.json"
CREDENTIAL = ROOT / "secrets" / "intraforge_credential"

if not (SANDBOX.exists() and CONFIG.exists() and CREDENTIAL.exists()):
    pytest.skip("IntraForge sandbox is not set up", allow_module_level=True)

sys.path.insert(0, str(ROOT / "scripts"))
from intraforge_admin_session import IntraForgeAdmin  # noqa: E402


@pytest.fixture
def sandbox():
    data = json.loads(SANDBOX.read_text(encoding="utf-8"))
    admin = IntraForgeAdmin(data["base_url"], ca_bundle=data["ca_bundle"])
    admin.login(data["email"], data["password"])
    yield data, admin
    admin.close()


@pytest.fixture
def live(world, monkeypatch):
    """Point this test's pilot at the real IntraForge sandbox instead of the fake."""
    monkeypatch.setattr(intraforge, "DEFAULT_TRANSPORT", None)
    monkeypatch.setenv("IFPAY_INTRAFORGE_CREDENTIAL_FILE", str(CREDENTIAL))
    cfg = IntraForgeConfig.validate(json.loads(CONFIG.read_text(encoding="utf-8")))
    with db.connection() as conn:
        intraforge.save_config(conn, config=cfg, actor_id=None)
    return cfg


def _bearer(cfg):
    return httpx.Client(base_url=cfg.base_url, verify=cfg.ca_bundle, timeout=30,
                        headers={"Authorization": f"Bearer {CREDENTIAL.read_text(encoding='utf-8').strip()}"})


def test_live_demo_receipt_lands_in_the_bucket_and_in_intraforge_audit(live, sandbox):
    data, admin = sandbox
    status = Session("admin").post("/api/admin/integration/verify").json()
    assert status["ok"] is True, status

    cashier, customer = Session("cashier"), Session("customer1")
    pr = cashier.post("/api/shop/payment-requests", json={"amount": "25"}).json()
    assert pr["merchant"]["verified"] is True
    paid = customer.post("/api/wallet/checkout/pay", json={"code": pr["code"], "expected_amount_minor": 2500,
                                                           "confirm": True}).json()
    assert paid["balance"] == "75.00" and balance_of("customer1") == 7_500
    number = paid["receipt"]["receipt_number"]

    uploads = cashier.get(f"/api/receipts/{number}").json()["uploads"]
    assert len(uploads) == 1, "the receipt should have been stored in IntraForge after the payment"
    file_id = uploads[0]["intraforge_file_id"]

    # The stored file is in the Pilot Shop Receipts bucket and is byte-identical to the receipt.
    with _bearer(live) as http:
        meta = http.get(f"/api/v1/files/{file_id}/").json()
        content = http.get(f"/api/v1/files/{file_id}/download/").content
    assert meta["bucket"] == live.bucket_id and meta["display_filename"] == f"{number}.txt"
    with db.connection() as conn:
        receipt = conn.execute("SELECT * FROM receipts WHERE receipt_number = %s", (number,)).fetchone()
    assert hashlib.sha256(content).hexdigest() == hashlib.sha256(receipts.render_text(receipt)).hexdigest()
    assert receipt["body_sha256"] in content.decode()

    # The IntraForge administrator sees the upload, by the Pilot Shop service account, in IntraForge's own audit log.
    events = admin.results(admin.ok("GET", f"organizations/{data['organization_id']}/audit/",
                                    params={"action": "storage.file.upload", "resource_id": file_id}))
    assert len(events) == 1 and events[0]["result"] == "success"
    assert events[0]["context"]["display_filename"] == f"{number}.txt"
    with _bearer(live) as http:
        service_user = http.get("/api/v1/auth/me/").json()
    assert str(events[0]["actor"]) == str(service_user["id"])


def test_live_unauthorized_bucket_access_is_refused(live, sandbox):
    data, admin = sandbox
    other = admin.ok("POST", f"projects/{data['project_id']}/buckets/",
                     json={"name": f"Not for Pilot Shop {uuid.uuid4().hex[:6]}"})
    admin_file = admin.ok("POST", f"buckets/{other['id']}/files/", files={"file": ("private.txt", b"private")})
    cfg = IntraForgeConfig.validate({**json.loads(CONFIG.read_text(encoding="utf-8")), "bucket_id": other["id"]})
    token = CREDENTIAL.read_text(encoding="utf-8").strip()
    with IntraForgeClient(cfg, token) as client:
        for call in (client.verify, client.list_files,
                     lambda: client.upload(filename="x.txt", content=b"x", mime_type="text/plain"),
                     lambda: client.get_file(admin_file["id"])):
            with pytest.raises(IntegrationError) as exc:
                call()
            assert exc.value.code == "not_authorized"
    with _bearer(live) as http:
        assert http.get(f"/api/v1/files/{admin_file['id']}/download/").status_code == 403
        # Least privilege on its own bucket too: no bucket creation, no deletion, no audit/application access.
        assert http.post(f"/api/v1/projects/{data['project_id']}/buckets/", json={"name": "nope"}).status_code == 403
        own = http.post(f"/api/v1/buckets/{live.bucket_id}/files/", files={"file": ("probe.txt", b"probe")}).json()
        assert http.delete(f"/api/v1/files/{own['id']}/").status_code == 403
        assert http.get(f"/api/v1/organizations/{data['organization_id']}/audit/").status_code == 403
        assert http.get(f"/api/v1/organizations/{data['organization_id']}/applications/").status_code == 403


def test_live_revoked_credential_is_rejected(live, sandbox):
    data, admin = sandbox
    issued = admin.ok("POST", f"applications/{live.application_id}/credentials/")
    with IntraForgeClient(live, issued["secret"]) as client:
        assert client.verify()["application_id"] == live.application_id
    admin.ok("POST", f"applications/{live.application_id}/credentials/{issued['id']}/revoke/")
    with IntraForgeClient(live, issued["secret"]) as client:
        with pytest.raises(IntegrationError) as exc:
            client.verify()
        assert exc.value.code == "credential_rejected"
        with pytest.raises(IntegrationError) as exc:
            client.upload(filename="after-revoke.txt", content=b"x", mime_type="text/plain")
        assert exc.value.code == "credential_rejected"
    # The shop's own credential is unaffected.
    with IntraForgeClient(live, CREDENTIAL.read_text(encoding="utf-8").strip()) as client:
        assert client.verify()["bucket_id"] == live.bucket_id


def test_live_garbage_credential_is_rejected(live):
    with IntraForgeClient(live, f"pdc_sk_{uuid.uuid4().hex}.forged") as client:
        with pytest.raises(IntegrationError) as exc:
            client.verify()
    assert exc.value.code == "credential_rejected"
