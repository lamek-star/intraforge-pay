"""This shop PC's own HTTPS identity, and the "connect to this shop" code the customer app pairs with.

Every shop PC creates its OWN local certificate authority (kept under <secret dir>/tls, reused on later runs) and a
server certificate for its names and addresses. Nothing is built into the Android app: the app learns which
certificate authority to trust by scanning the shop's connect QR in person (certificate pinning), so one app works with
any number of shop PCs, each identified by its shop name.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import ipaddress
import json
import socket
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

PAIRING_PREFIX = "IFPAYSHOP1."
EMULATOR_HOST = "10.0.2.2"  # the Android emulator's address for the PC it runs on


def _write_key(path: Path, key) -> None:
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    try:
        path.chmod(0o600)
    except OSError:
        pass


def ensure_ca(tls_dir: Path):
    """Returns (key, cert, created). An existing CA is always reused, so paired phones keep working."""
    key_path, crt_path = tls_dir / "ca.key", tls_dir / "ca.crt"
    if key_path.exists() and crt_path.exists():
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        return key, x509.load_pem_x509_certificate(crt_path.read_bytes()), False
    tls_dir.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"IntraForge Pay shop CA ({socket.gethostname()})"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "IntraForge Pay pilot (simulated value only)")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=730))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    _write_key(key_path, key)
    crt_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return key, cert, True


def local_ipv4s() -> list[str]:
    try:
        return sorted({a for a in socket.gethostbyname_ex(socket.gethostname())[2] if not a.startswith("127.")},
                      key=ipaddress.ip_address)
    except OSError:
        return []


def issue_server_cert(tls_dir: Path, *, extra_names=(), extra_ips=()) -> dict:
    ca_key, ca_cert, created = ensure_ca(tls_dir)
    names = sorted({"localhost", socket.gethostname().lower(), *[n.lower() for n in extra_names]})
    ips = sorted({"127.0.0.1", EMULATOR_HOST, *local_ipv4s(), *extra_ips}, key=ipaddress.ip_address)
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "IntraForge Pay shop server")]))
            .issuer_name(ca_cert.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=397))
            .add_extension(x509.SubjectAlternativeName([*[x509.DNSName(n) for n in names],
                                                        *[x509.IPAddress(ipaddress.ip_address(i)) for i in ips]]),
                           critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    _write_key(tls_dir / "server.key", key)
    # The chain includes the CA, so a phone can find the pinned CA in what the server presents.
    (tls_dir / "server.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM)
                                         + ca_cert.public_bytes(serialization.Encoding.PEM))
    return {"ca_created": created, "names": names, "ips": ips}


def ca_pin(tls_dir: Path) -> str:
    """'sha256/<base64>' of the CA's SubjectPublicKeyInfo -- the standard public-key pin format."""
    cert = x509.load_pem_x509_certificate((tls_dir / "ca.crt").read_bytes())
    spki = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return "sha256/" + base64.b64encode(hashlib.sha256(spki).digest()).decode()


def server_urls(tls_dir: Path, port: int, preferred_hosts: list[str] | None = None) -> list[str]:
    """Addresses a phone may use, from the server certificate: network IPs first, then names, the emulator last.

    `preferred_hosts` (IFPAY_PUBLIC_HOSTS, set by run_shop_server.ps1 -Lan to the shop network's address) replaces the
    full list, so phones do not first try virtual-adapter addresses; each must be in the certificate, or it is ignored."""
    pem = (tls_dir / "server.crt").read_bytes()
    cert = x509.load_pem_x509_certificate(pem)  # the first certificate in the file is the server's
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    ips = [str(i) for i in san.get_values_for_type(x509.IPAddress)]
    names = [n for n in san.get_values_for_type(x509.DNSName) if n != "localhost"]
    preferred = [h for h in (preferred_hosts or []) if h in ips or h.lower() in names]
    if preferred:
        ordered = preferred
    else:
        ordered = [i for i in ips if not i.startswith("127.") and i != EMULATOR_HOST] + names
    if EMULATOR_HOST in ips and EMULATOR_HOST not in ordered:
        ordered = ordered + [EMULATOR_HOST]
    return [f"https://{h}:{port}" for h in ordered]


def preferred_hosts_from_env() -> list[str]:
    import os

    return [h.strip() for h in os.environ.get("IFPAY_PUBLIC_HOSTS", "").split(",") if h.strip()]


def pairing_code(*, shop_name: str, merchant_id: str, tls_dir: Path, port: int) -> str:
    payload = {"v": 1, "name": shop_name, "merchant_id": merchant_id,
               "urls": server_urls(tls_dir, port, preferred_hosts_from_env()),
               "pin": ca_pin(tls_dir)}
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    return PAIRING_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_pairing_code(code: str) -> dict:
    if not code.startswith(PAIRING_PREFIX):
        raise ValueError("not a shop connect code")
    body = code[len(PAIRING_PREFIX):]
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
