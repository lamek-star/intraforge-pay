"""The shop's "connect" code for the customer app: shop name, HTTPS addresses, and the pin of this PC's own CA."""

import base64
import hashlib

from conftest import Session
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from ifpay import config, tls


def _independent_pin(ca_path) -> str:
    cert = x509.load_pem_x509_certificate(ca_path.read_bytes())
    spki = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return "sha256/" + base64.b64encode(hashlib.sha256(spki).digest()).decode()


def test_connect_code_identifies_the_shop_and_pins_this_pcs_ca(world):
    tls_dir = config.get_settings().tls_dir
    tls.issue_server_cert(tls_dir, extra_ips=["192.168.1.20"])
    r = Session("cashier").get("/api/shop/pairing")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["shop"] == "Pilot Shop" and body["qr_svg"].startswith("data:image/svg+xml")
    payload = tls.decode_pairing_code(body["code"])
    assert payload["v"] == 1 and payload["name"] == "Pilot Shop"
    assert payload["merchant_id"] == str(world["merchant"]["id"])
    assert payload["pin"] == _independent_pin(tls_dir / "ca.crt")
    assert all(u.startswith("https://") and u.endswith(":8643") for u in payload["urls"])
    assert "https://192.168.1.20:8643" in payload["urls"]
    assert payload["urls"][-1] == "https://10.0.2.2:8643"           # emulator address last
    assert not any("127.0.0.1" in u or "localhost" in u for u in payload["urls"])


def test_renewing_the_server_certificate_keeps_the_pin(world):
    tls_dir = config.get_settings().tls_dir
    tls.issue_server_cert(tls_dir)
    first = tls.ca_pin(tls_dir)
    result = tls.issue_server_cert(tls_dir, extra_ips=["10.1.2.3"])
    assert result["ca_created"] is False and tls.ca_pin(tls_dir) == first


def test_connect_code_is_for_shop_staff_and_needs_https(world):
    assert Session("customer1").get("/api/shop/pairing").status_code == 403
    assert Session().get("/api/shop/pairing").status_code == 401
    r = Session("cashier").get("/api/shop/pairing")  # no certificate created in this test
    assert r.status_code == 503 and r.json()["code"] == "https_not_set_up"


def test_shop_identity_is_public(world):
    r = Session().get("/api/shop-identity")
    assert r.status_code == 200
    assert r.json() == {"name": "Pilot Shop", "merchant_id": str(world["merchant"]["id"])}


def test_shop_network_addresses_can_replace_the_full_list(world, monkeypatch):
    tls_dir = config.get_settings().tls_dir
    tls.issue_server_cert(tls_dir, extra_ips=["192.168.1.20", "172.19.0.1"])
    monkeypatch.setenv("IFPAY_PUBLIC_HOSTS", "192.168.1.20, 203.0.113.9")  # the second is not in the certificate
    payload = tls.decode_pairing_code(Session("cashier").get("/api/shop/pairing").json()["code"])
    assert payload["urls"] == ["https://192.168.1.20:8643", "https://10.0.2.2:8643"]
