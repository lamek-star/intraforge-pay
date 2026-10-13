"""The IntraForge integration against an in-process fake with IntraForge's verified semantics: revoked credentials,
unauthorized bucket access, the wrong application, IntraForge being down, and the configuration wizard's secret
handling. The same behaviours are checked against a real IntraForge in test_live_intraforge.py."""

import httpx
import pytest
from conftest import Session, balance_of

from ifpay import config, db, documents
from ifpay.intraforge import IntraForgeClient, IntraForgeConfig, IntegrationError


def _expire_status():
    with db.connection() as conn:
        conn.execute("UPDATE integration_status SET checked_at = now() - interval '1 hour'")


def test_revoked_credential_blocks_new_qr_codes_and_uploads_but_not_authorized_payments(world):
    fake = world["intraforge"]
    cashier, customer = Session("cashier"), Session("customer1")
    pr = cashier.post("/api/shop/payment-requests", json={"amount": "25"}).json()  # issued while verified

    fake.revoke(fake.token)
    _expire_status()

    # The shop can no longer present itself as verified.
    r = cashier.post("/api/shop/payment-requests", json={"amount": "5"})
    assert r.status_code == 503 and r.json()["code"] == "merchant_not_verified"
    status = Session("admin").get("/api/admin/integration").json()["status"]
    assert status["ok"] is False and status["detail"]["code"] == "credential_rejected"

    # Paying an already-issued, verified QR is a ledger decision, not a document upload: it still settles...
    paid = customer.post("/api/wallet/checkout/pay", json={"code": pr["code"], "expected_amount_minor": 2500,
                                                           "confirm": True})
    assert paid.status_code == 200 and balance_of("customer1") == 7_500
    number = paid.json()["receipt"]["receipt_number"]
    # ...but its receipt could not be stored, and an explicit upload is refused by IntraForge.
    assert customer.get(f"/api/receipts/{number}").json()["uploads"] == []
    r = cashier.post(f"/api/shop/receipts/{number}/upload")
    assert r.status_code == 502 and r.json()["code"] == "intraforge_credential_rejected"
    assert fake.files == {}

    # A newly issued credential (rotation) restores everything without a restart.
    config.get_settings().intraforge_credential_file.write_text(fake.issue(), encoding="utf-8")
    assert Session("admin").post("/api/admin/integration/verify").json()["ok"] is True
    assert cashier.post(f"/api/shop/receipts/{number}/upload").status_code == 200
    assert len(fake.files) == 1


def test_bucket_without_a_grant_is_refused(world):
    fake = world["intraforge"]
    with db.connection() as conn:
        client_cfg = IntraForgeConfig.validate({**fake.config(), "bucket_id": fake.other_bucket_id})
    with IntraForgeClient(client_cfg, fake.token, transport=fake.transport()) as client:
        with pytest.raises(IntegrationError) as exc:
            client.verify()
        assert exc.value.code == "not_authorized"
        with pytest.raises(IntegrationError) as exc:
            client.upload(filename="x.txt", content=b"x", mime_type="text/plain")
        assert exc.value.code == "not_authorized"
    assert fake.files == {}

    # Pointing the shop at a bucket it was not granted makes it unverified.
    r = Session("admin").put("/api/admin/integration", json={**fake.config(), "bucket_id": fake.other_bucket_id})
    assert r.status_code == 200 and r.json()["status"]["ok"] is False
    assert r.json()["status"]["code"] == "not_authorized"
    assert Session("cashier").post("/api/shop/payment-requests", json={"amount": "1"}).status_code == 503


def test_read_only_grant_cannot_upload(world):
    fake = world["intraforge"]
    fake.grants.discard(("storage.write", fake.bucket_id))
    cashier, customer = Session("cashier"), Session("customer1")
    pr = cashier.post("/api/shop/payment-requests", json={"amount": "2"}).json()
    number = customer.post("/api/wallet/checkout/pay", json={"code": pr["code"], "expected_amount_minor": 200,
                                                             "confirm": True}).json()["receipt"]["receipt_number"]
    r = cashier.post(f"/api/shop/receipts/{number}/upload")
    assert r.status_code == 502 and r.json()["code"] == "intraforge_not_authorized"


def test_credential_of_another_application_is_not_accepted(world):
    fake = world["intraforge"]
    fake.application_id = "11111111-1111-1111-1111-111111111111"  # the token now belongs to a different app
    _expire_status()
    result = documents.verify_now(actor=None)
    assert result["ok"] is False and result["code"] == "wrong_application"


def test_intraforge_unreachable_is_reported_and_payments_still_settle(world):
    fake = world["intraforge"]
    cashier, customer = Session("cashier"), Session("customer1")
    pr = cashier.post("/api/shop/payment-requests", json={"amount": "3"}).json()
    fake.down = True
    r = customer.post("/api/wallet/checkout/pay", json={"code": pr["code"], "expected_amount_minor": 300,
                                                        "confirm": True})
    assert r.status_code == 200 and balance_of("customer1") == 9_700
    number = r.json()["receipt"]["receipt_number"]
    with db.connection() as conn:
        failure = conn.execute("SELECT * FROM audit_events WHERE action = 'intraforge.receipt.upload' "
                               "AND result = 'error'").fetchone()
    assert failure["subject_id"] == number and failure["detail"]["code"] == "unavailable"
    fake.down = False
    assert cashier.post(f"/api/shop/receipts/{number}/upload").status_code == 200


def test_missing_credential_file(world):
    config.get_settings().intraforge_credential_file.unlink()
    _expire_status()
    assert documents.verify_now(actor=None)["code"] == "credential_missing"


def test_supporting_document_upload(world):
    fake = world["intraforge"]
    cashier, customer = Session("cashier"), Session("customer1")
    pr = cashier.post("/api/shop/payment-requests", json={"amount": "4"}).json()
    number = customer.post("/api/wallet/checkout/pay", json={"code": pr["code"], "expected_amount_minor": 400,
                                                             "confirm": True}).json()["receipt"]["receipt_number"]
    r = cashier.post(f"/api/shop/receipts/{number}/documents", content=b"%PDF-1.4 test",
                     headers={"Content-Type": "application/pdf", "X-Filename": "../../delivery note.pdf"})
    assert r.status_code == 200, r.text
    assert r.json()["filename"] == f"{number}-support-delivery note.pdf"
    r = cashier.post(f"/api/shop/receipts/{number}/documents", content=b"<script>",
                     headers={"Content-Type": "text/html", "X-Filename": "x.html"})
    assert r.status_code == 400 and r.json()["code"] == "unsupported_type"
    # Customers may view receipts but not store documents in the shop's bucket.
    r = customer.post(f"/api/shop/receipts/{number}/documents", content=b"x",
                      headers={"Content-Type": "text/plain"})
    assert r.status_code == 403
    assert sum(1 for f in fake.files.values() if "support" in f["display_filename"]) == 1


@pytest.mark.parametrize("bad,fragment", [
    ({"credential": "pdc_sk_abc.def"}, "secret"),
    ({"base_url": "https://x.test", "note": "pdc_sk_0123.zzz"}, "secret"),
    ({"base_url": "http://plain.test"}, "https://"),
    ({"base_url": "https://x.test/api"}, "https://"),
    ({"bucket_id": "not-a-uuid"}, "UUID"),
    ({"surprise": "1"}, "Unknown"),
])
def test_configuration_wizard_rejects_secrets_and_bad_values(world, bad, fragment):
    payload = {**world["intraforge"].config(), **bad}
    r = Session("admin").put("/api/admin/integration", json=payload)
    assert r.status_code == 400 and fragment in r.json()["message"]


def test_credential_never_leaves_the_server(world):
    fake = world["intraforge"]
    admin = Session("admin")
    pages = [admin.get("/api/admin/integration").text, admin.get("/api/admin/audit").text]
    secret_part = fake.token.split(".", 1)[1]
    for text in pages:
        assert secret_part not in text and "pdc_sk_" not in text
    with db.connection() as conn:
        dumped = " ".join(str(r) for t in ("integration_settings", "integration_status", "audit_events")
                          for r in conn.execute(f"SELECT * FROM {t}"))
    assert secret_part not in dumped and "pdc_sk_" not in dumped


def test_unknown_transport_error_is_unavailable(world):
    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)

    cfg = IntraForgeConfig.validate(world["intraforge"].config())
    with IntraForgeClient(cfg, world["intraforge"].token, transport=httpx.MockTransport(boom)) as client:
        with pytest.raises(IntegrationError) as exc:
            client.verify()
    assert exc.value.code == "unavailable" and world["intraforge"].token not in str(exc.value)
