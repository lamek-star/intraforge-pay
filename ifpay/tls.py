"""The "connect to this shop" code the customer phone app pairs with.

Phones reach Pay through IntraForge's own HTTPS proxy (Caddy, ``tls internal``), at IntraForge's address plus Pay's
path (e.g. https://192.168.1.10:8443/apps/pay-1). The code carries the shop's name and id, those addresses, and
IntraForge's certificate authority (the proxy's root CA) together with its pin (SHA-256 of its public key). The app
stores it and afterwards trusts only a server whose certificate chain validates against exactly that CA, and that
answers with the same shop id. The proxy does not send its root CA in the handshake (only its server and intermediate
certificates), so the CA itself travels in the code; it was obtained in person, at the counter, so a look-alike server
on the same Wi-Fi cannot impersonate the shop. Nothing is installed system-wide on the phone.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization

PAIRING_PREFIX = "IFPAYSHOP1."


def load_ca(path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(path.read_bytes())


def ca_pin(cert: x509.Certificate) -> str:
    """'sha256/<base64>' of the CA's SubjectPublicKeyInfo -- the standard public-key pin format."""
    spki = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return "sha256/" + base64.b64encode(hashlib.sha256(spki).digest()).decode()


def shop_urls(public_urls: tuple[str, ...], base_path: str) -> list[str]:
    """HTTPS addresses only: the app refuses anything else."""
    return [u.rstrip("/") + base_path for u in public_urls if u.lower().startswith("https://")]


def pairing_code(*, shop_name: str, merchant_id: str, urls: list[str], ca: x509.Certificate) -> str:
    payload = {"v": 2, "name": shop_name, "merchant_id": merchant_id, "urls": urls, "pin": ca_pin(ca),
               "ca": base64.b64encode(ca.public_bytes(serialization.Encoding.DER)).decode()}
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    return PAIRING_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_pairing_code(code: str) -> dict:
    if not code.startswith(PAIRING_PREFIX):
        raise ValueError("not a shop connect code")
    body = code[len(PAIRING_PREFIX):]
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
