"""HTTPS for this shop PC, so phones talk to it encrypted (shop Wi-Fi is not trusted).

Creates, under secrets/tls/ (or $IFPAY_SECRET_DIR/tls):
  ca.key / ca.crt          this shop PC's own certificate authority (created once, then always reused, so phones that
                           already paired with this shop keep working)
  server.key / server.crt  the server certificate, for: localhost, this PC's name, 127.0.0.1, 10.0.2.2 (the Android
                           emulator's address for this PC) and this PC's network addresses (plus --ip/--name)

Nothing is copied into the Android app: customers pair with the shop by scanning its "connect" QR (shop page).
Re-run this when the shop PC's network address changes, then customers re-scan the connect QR.

  python scripts/make_tls.py [--ip 192.168.1.20] [--name shop-pc]
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ifpay import tls  # noqa: E402
from ifpay.config import get_settings  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ip", action="append", default=[], help="extra IP address of the shop PC (repeatable)")
    parser.add_argument("--name", action="append", default=[], help="extra host name of the shop PC (repeatable)")
    args = parser.parse_args()
    tls_dir = get_settings().secret_dir / "tls"
    result = tls.issue_server_cert(tls_dir, extra_names=args.name, extra_ips=args.ip)
    print(("Created" if result["ca_created"] else "Reused") + f" this shop PC's certificate authority: {tls_dir / 'ca.crt'}")
    print(f"Server certificate for: {', '.join(result['names'] + result['ips'])}")
    print(f"Shop identity pin: {tls.ca_pin(tls_dir)}")


if __name__ == "__main__":
    main()
