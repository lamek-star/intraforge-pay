# IntraForge Pay — Single-Shop Pilot: Installation and Usage Manual

> **SIMULATED VALUE — NO CASH VALUE.** Every balance, checkout and receipt in this pilot is a simulated IF Credit.
> There is no real money, no banking, no cash top-up, no merchant payout and no offline payment. Do not deploy it to
> production and do not use it with real customer funds.

## 1. What the pilot is

One shop ("Pilot Shop"), one cashier, five test customers and one IntraForge administrator. The cashier types an
amount and shows a QR code; a customer scans it, sees the verified shop and the amount, confirms, and both receive the
same receipt. The receipt is stored in an IntraForge bucket the shop is authorized for.

```
 Browser (shop / wallet / admin)                     IntraForge (existing platform, unchanged)
        │  session cookie + CSRF                              ▲
        ▼                                                     │ Bearer pdc_sk_… (Application credential)
 Pilot Shop server  (this project, FastAPI)  ─────────────────┘ only: auth/me, bucket folders/files
        │                                                       storage.read + storage.write on ONE bucket
        ▼
 Pilot PostgreSQL  (own container, port 55432 on loopback)
   double-entry journal · receipts · maker/checker · append-only audit
```

| Concern | Where it lives | Why |
|---|---|---|
| Payment authority | Pilot server: customer session + explicit confirmation + the journal | IntraForge has no payment capability; uploading a document must never authorize a payment |
| Balances | Computed only from the journal (`account_balances` view) | No stored balance exists to update without a journal transaction |
| Ledger rules | Enforced by PostgreSQL triggers/constraints, and again by the code | A code bug cannot unbalance the ledger or overdraw a wallet |
| Shop identity | IntraForge Application "Pilot Shop" + its credential | The shop is "verified" only while IntraForge accepts its credential for that application and bucket |
| Receipts / documents | IntraForge bucket "Pilot Shop Receipts" via the bucket API | Storage only; a failed upload never affects a payment and can be retried |
| Application secret | `secrets/intraforge_credential` on the shop server | Never in the browser, the database, logs, or the configuration file |

The IntraForge repository was **not modified**. Everything uses its existing, verified API (see section 9).

## 2. Prerequisites

- Windows 10/11 (or Linux/macOS) with **Docker Desktop** running.
- **Python 3.12** on the shop server.
- An IntraForge installation, either:
  - **A. the bundled sandbox** (`intraforge-sandbox/`, recommended for the pilot): a separate, throw-away IntraForge
    with its own Docker project (`ifpay-intraforge-sandbox`), its own volumes and port `9443`. It uses the
    `intraforge-shop-*:1.2.0-6fc50c752` images already on this PC and **never touches any other IntraForge
    installation or its data**; or
  - **B. an existing IntraForge** where you have an Organization Administrator account (without MFA for the scripted
    setup) and a project to hold the receipts bucket.

## 3. Installation

All commands run from the project folder (`C:\Users\Hp\projects\intraforge-pay-pilot`) in PowerShell.

### 3.1 Python environment and the pilot database

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python scripts\init_local.py          # writes .env with a generated DB password (never overwrites)
docker compose --env-file .env up -d                   # the pilot's own PostgreSQL (127.0.0.1:55432)
```

### 3.2 IntraForge (option A — sandbox)

```powershell
cd intraforge-sandbox
..\.venv\Scripts\python make_env.py                    # generated secrets in intraforge-sandbox\.env
docker compose up -d postgres-control postgres-tenant valkey object-storage
docker compose run --rm backend python manage.py migrate --noinput
docker compose run --rm backend python manage.py seed_permissions
docker compose up -d
docker compose cp proxy:/data/caddy/pki/authorities/local/root.crt ./caddy-root.crt   # IntraForge's HTTPS CA
..\.venv\Scripts\python bootstrap.py                   # IntraForge admin, organization, workspace, project
cd ..
```

Wait until `curl.exe -sk https://localhost:9443/readyz` answers 200 before running `bootstrap.py`. The sandbox admin
credentials are written to `intraforge-sandbox\.sandbox-admin.json` (sandbox only).

### 3.3 IntraForge administrator setup (manual requirement 1)

```powershell
# Sandbox:
.\.venv\Scripts\python scripts\intraforge_admin_setup.py --sandbox
# Existing IntraForge (prompts for the password):
.\.venv\Scripts\python scripts\intraforge_admin_setup.py --intraforge https://<host>:<port> `
    --ca-bundle <IntraForge CA .crt> --admin-email <you> --organization-id <org uuid> --project-id <project uuid>
```

Through IntraForge's own API this:

1. registers the application **Pilot Shop**;
2. issues a **development application credential** and writes it to `secrets\intraforge_credential` (the secret is
   never printed);
3. creates the bucket **Pilot Shop Receipts**;
4. grants the application **only** `storage.read` and `storage.write` on that one bucket (no role, no
   `storage.delete`/`storage.manage`, nothing on any other bucket — IntraForge has no payment capabilities to grant);
5. writes the **non-secret** configuration to `config\intraforge.json`.

Re-running is safe (existing application, bucket and grants are reused). `--rotate-credential` issues a new credential
and revokes the old one in IntraForge; the shop server picks it up without a restart.

### 3.4 Pilot accounts (synthetic test data)

```powershell
.\.venv\Scripts\python scripts\seed_pilot.py           # add --allocate to also do the maker/checker allocation
```

Creates `admin` (IntraForge administrator, maker), `supervisor` (checker), `cashier` (Pilot Shop) and `customer1` …
`customer5` (synthetic names, empty wallets). Generated passwords are in `secrets\test-accounts.json`.

> **Maker/checker needs two people.** The brief has one IntraForge administrator; the pilot adds a *Pilot Supervisor*
> as the checker. Nobody can approve their own request (enforced by the code and by a database `CHECK`).

### 3.5 Start the shop server

```powershell
.\.venv\Scripts\python -m uvicorn ifpay.web.app:app --host 127.0.0.1 --port 8600
```

Open <http://localhost:8600>. The server applies database migrations on start. It listens on the loopback address
only; see section 8 for phones.

### 3.6 Configuration wizard

Sign in as `admin` → **IntraForge connection** → choose `config\intraforge.json` (or paste it) → **Save and verify**.
The wizard accepts only the known non-secret fields and **refuses any file that contains a credential or anything that
looks like a secret**. Status must show **✓ Verified** and *Credential file on this server: present*.

## 4. Using the pilot

Use **two different host names** so each role keeps its own sign-in in one browser — e.g. the shop at
`http://localhost:8600` and the customer at `http://127.0.0.1:8600` (or use a private window).

### Administrator (`admin`, `supervisor`)
- **Overview** — credits issued, held by customers, owed to the shop, and the **ledger conservation** check
  (debits = credits; issued = customers + shop; no unbalanced transaction; no negative wallet).
- **Credits (maker/checker)** — `admin` requests an allocation (e.g. *Request 100 for every customer without
  credits*); `supervisor` signs in and **Approves** (or Rejects). Only an approval posts to the journal:
  *Dr Issuance reserve / Cr Customer simulated liability*.
- **Customers** — create further test customers. **Users** — deactivate an account (signed out at once).
- **Transactions** — every journal transaction with its entries and receipt. **Audit log** — append-only.

### Shopkeeper (`cashier`)
Enter an amount → **Generate QR**. The page shows the QR, the amount, the status (*waiting → PAID / expired /
cancelled*, updated every 2 s), the payment code for typing/pasting, **Total simulated sales** (the merchant payable
balance), **Today's transactions** and a **Receipt** viewer with *Upload receipt to IntraForge* and *Attach supporting
document* (PDF, PNG, JPEG or text, ≤ 5 MB). A QR code expires after 120 s (`IFPAY_QR_TTL_SECONDS`).

### Customer (`customer1` … `customer5`)
Balance and history → **Scan QR** (live camera, a photo of the QR, or paste the code) → the confirmation screen shows
the **shop name, a "Verified by IntraForge" badge and the amount** → **Confirm and pay** → receipt. Nothing is charged
before the explicit confirmation, and the amount confirmed must equal the request.

## 5. The pilot demonstration (manual requirement 6)

Run it in the browser as in section 4, or let the script do all twelve steps against the running server and the real
IntraForge and print the evidence:

```powershell
.\.venv\Scripts\python scripts\demo_walkthrough.py      # on a freshly seeded pilot database
```

| # | Step | Evidence |
|---|---|---|
| 1 | Administrator creates/connects the Pilot Shop application | `intraforge_admin_setup.py` output; wizard *✓ Verified* |
| 2 | Test customers get 100 credits each (maker/checker) | Credits tab; the maker's own approval is refused (`self_approval`) |
| 3–4 | Shopkeeper enters 25, QR generated | Shop page |
| 5–6 | Customer scans, sees verified shop and amount | Wallet confirmation screen |
| 7 | Customer confirms | Payment refused without `confirm: true` |
| 8 | Customer 100 → 75 | Wallet balance |
| 9 | Merchant payable +25 | Shop *Total simulated sales*; Admin overview |
| 10 | Same receipt for both | Same receipt number and SHA-256 fingerprint on both screens |
| 11 | Receipt in the authorized bucket | Receipt viewer lists the IntraForge file id; file is `IFP-…​.txt` in *Pilot Shop Receipts* |
| 12 | Administrator sees it in the audit records | Pilot **Audit log** (`payment.confirm`) and IntraForge's own audit log (`storage.file.upload` by the Pilot Shop service account, filename = receipt number) |

Last recorded run (2026-10-13): **all checks passed** — receipt `IFP-20261013-000001`, IntraForge file
`7b29d3ac-…`, IntraForge audit event `8773c81c-…`, conservation 500.00 = 475.00 + 25.00.

**About step 12:** IntraForge has no API through which an application can write its own audit events. The IntraForge
administrator therefore sees each transaction (a) in the pilot's append-only audit log and journal, and (b) in
IntraForge's own audit log as the receipt-upload event of the Pilot Shop application, named after the receipt number.

## 6. Automated tests (manual requirement 7)

```powershell
.\.venv\Scripts\python -m pytest -m "not live"         # 79 tests, real PostgreSQL, ~2 min
.\.venv\Scripts\python -m pytest -m live               # 4 tests against the real IntraForge sandbox
```

Tests use their own databases cloned from a migrated template (`ifpay_test_*`), never the demo database `ifpay`.
`test_restart.py` restarts the pilot PostgreSQL container (`ifpay-pilot-postgres-1`); run it when a short database
restart is acceptable.

| Requirement | Tests |
|---|---|
| Insufficient balance | `test_checkout_rules::test_insufficient_balance_*`; DB-level `test_ledger_rules::test_customer_balance_can_never_go_negative_*` |
| Duplicate confirmation | `test_checkout_rules::test_duplicate_confirmation_*`; `test_concurrency::test_same_qr_confirmed_ten_times_at_once_*` |
| Expired QR | `test_checkout_rules::test_expired_qr_is_refused`, `test_database_refuses_to_mark_an_expired_request_paid` |
| Unauthorized users | `test_authorization.py` (no session, wrong role, other customer's/shop's receipts, CSRF, deactivated user, login rate limit) |
| Revoked credentials | `test_intraforge_integration::test_revoked_credential_*`; live `test_live_revoked_credential_is_rejected`, `test_live_garbage_credential_is_rejected` |
| Simultaneous payments | `test_concurrency.py` (10× same QR, 5 customers on one QR, 6 QRs against 100.00, 20 mixed) |
| Unauthorized bucket access | `test_intraforge_integration::test_bucket_without_a_grant_*`, `test_read_only_grant_cannot_upload`; live `test_live_unauthorized_bucket_access_is_refused` (also: no bucket creation, no delete, no audit/application access) |
| Ledger conservation | `test_ledger_rules::test_ledger_conservation_*`, conservation asserted in flow and concurrency tests |
| Persistence after restart | `test_restart.py` (server process restart **and** PostgreSQL container restart; journal and receipts byte-identical) |
| Immutability, balancing | `test_ledger_rules.py` (unbalanced, single-entry, negative, UPDATE/DELETE/TRUNCATE refused) |
| Maker/checker | `test_allocations.py` |
| Full demo | `test_payment_flow.py`, live `test_live_demo_receipt_lands_in_the_bucket_and_in_intraforge_audit` |

Last full run (2026-10-13): **83 passed** (79 + 4 live). Dependency audit: `pip-audit -r requirements.txt` — no known
vulnerabilities. **DAST (HawkScan): blocked, not run** — no `hawk` CLI and no StackHawk API key on this PC; run it
before any exposure beyond the loopback address.

## 7. Security design (summary)

- **Authorization is capability-based** (`ifpay/security.py`), checked server-side on every request; object-level checks
  for receipts and payment requests (a foreign receipt answers 404, exactly like a missing one).
- **Sessions**: random token, stored hashed; cookie `HttpOnly`, `SameSite=Strict`; CSRF token required on every state
  change; 8-hour expiry; deactivation revokes sessions; 10 failed sign-ins per 5 min per user+address are blocked.
- **Passwords**: scrypt. **Content Security Policy** without inline script or style; `X-Frame-Options: DENY`.
- **QR codes** are HMAC-signed (key in `secrets\signing_key`, created on first start) and carry no authority: the server
  re-checks the request, its state, expiry, amount, the shop and the customer's balance under row locks.
- **Ledger**: deferred PostgreSQL constraint triggers reject unbalanced or single-entry transactions and negative
  customer balances at commit (they lock the account row themselves, so concurrent payments are serialized even if the
  application did not lock); journal, receipts, uploads and audit rows reject UPDATE/DELETE/TRUNCATE.
- **IntraForge credential**: read from the server file at each use, sent only in the `Authorization` header to the
  configured `https://` IntraForge with certificate verification against its CA; never logged or stored in the database.

## 8. Limits of this pilot (by design)

- Online only; no offline payments, live banking, cash top-ups, refunds or merchant payouts.
- One process: the login rate limiter and the per-receipt upload lock are per process.
- Served over plain HTTP on the loopback address. A phone on the shop's network needs HTTPS for the live camera
  (the *Take or choose a photo* scan works without it); put the server behind TLS before exposing it beyond this PC.
- The shop is verified at QR creation (at most 60 s old, `IFPAY_INTRAFORGE_VERIFY_MAX_AGE`). A QR issued while
  verified can still be paid if the credential is revoked afterwards; its receipt upload then fails and can be retried
  after rotation — payment authority is deliberately independent of document storage.
- "Today" uses `IFPAY_TIMEZONE` (default UTC).

## 9. IntraForge API used (verified against the source and a live 1.2.0 instance)

| Call | Who | Notes |
|---|---|---|
| `POST auth/register/`, `auth/login/`, `organizations/`, `…/workspaces/`, `…/projects/` | sandbox bootstrap (human admin) | session + CSRF |
| `POST organizations/{org}/applications/` | admin setup | needs `application.create` |
| `POST applications/{app}/credentials/`, `…/{id}/revoke/` | admin setup / tests | needs `application.credentials.manage`; secret shown once |
| `POST projects/{project}/buckets/` | admin setup | needs `storage.manage` |
| `POST applications/{app}/resource-grants/` | admin setup | `{"permission_code","resource_type":"storage.bucket","resource_id"}` |
| `GET auth/me/` | shop server (bearer) | returns `service-account+<application id>@service.internal` |
| `GET buckets/{id}/folders/`, `POST buckets/{id}/files/` | shop server (bearer) | `storage.read` / `storage.write` on the bucket; multipart `file` + `display_filename` |
| `GET organizations/{org}/audit/?action=&resource_id=` | IntraForge admin | `audit.read` |

**Found during live testing:** IntraForge answers a revoked, expired or forged bearer token with **HTTP 403**
(`error.code = "authentication_failed"`), not 401 — DRF takes the `WWW-Authenticate` header from its first
authenticator (`SessionAuthentication`). A missing grant is 403 `permission_denied` (or an empty 403). The client
distinguishes the two by the error code.

## 10. Operations

- **Stop**: Ctrl+C the server; `docker compose stop` (pilot DB); `cd intraforge-sandbox; docker compose stop`.
- **Start again**: `docker compose --env-file .env up -d`, then 3.5. Data persists in the Docker volumes.
- **Start the pilot over**: stop the server, then
  `.\.venv\Scripts\python scripts\reset_demo_database.py --yes-delete-all-pilot-data`, then 3.4 and 3.6.
- **Remove the sandbox completely** (deletes only the sandbox's data):
  `cd intraforge-sandbox; docker compose down -v`.

## 11. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| *The shop's IntraForge connection is not verified* when generating a QR | Admin → IntraForge connection shows the reason: `credential_rejected` (revoked → rerun setup with `--rotate-credential`), `not_authorized` (grant missing), `unavailable` (IntraForge down / wrong address / CA). |
| `CERTIFICATE_VERIFY_FAILED` | `ca_bundle` must point at the CA that signed IntraForge's certificate (sandbox: `intraforge-sandbox\caddy-root.crt`). |
| Receipt *not stored in the IntraForge bucket yet* | Upload happens right after payment; if IntraForge was unreachable, use *Upload receipt to IntraForge*. Failures are in the audit log. |
| Signing in as the customer signs out the cashier | Use a different host name (localhost vs 127.0.0.1) or a private window. |
| `IFPAY_DATABASE_URL … is not set` | Run `scripts\init_local.py` (creates `.env`). |

## 12. Android customer wallet app (`android/`)

A native Kotlin app (Jetpack Compose) for **customers**. **One app works with any shop PC**: the customer adds a shop
once by scanning that shop's **connect QR**, and from then on picks the shop **by its name** ("Pilot Shop") — no server
address to type, and no special build per shop. A phone can hold several shops. In the app: choose shop → sign in →
balance and history → **Scan QR** (or type the code) → shop name, *Verified by IntraForge*, amount and expiry
countdown → **Confirm and pay** → receipt (same SHA-256 fingerprint as the shop's copy). Every screen carries
**SIMULATED VALUE — NO CASH VALUE**. The shop keeps using the `/shop` web page.

### 12.1 How a phone recognises a shop (and why it is safe)

Each shop PC creates **its own** certificate authority (`scripts/make_tls.py`, kept in `secrets/tls/` and reused).
The shop page (**Customer app → Show connect QR**) shows a code containing: the shop's name and id, the shop PC's
HTTPS address(es), and the **pin** (SHA-256 of the public key) of that PC's certificate authority. The app stores this
and afterwards talks only to a server whose certificate chain leads to that exact pinned authority, and that answers
with the same shop id (`/api/shop-identity`). Because the pin is obtained in person, at the counter, a look-alike server
on the same Wi-Fi cannot impersonate the shop. HTTPS only; no plain HTTP anywhere; nothing is installed system-wide.

Stored on the phone: the paired shops (name, id, addresses, pin — all shown publicly on the shop screen), the last
used shop and username. Never the password or the session (closing the app signs out).

### 12.2 Shop PC (any Windows PC with the pilot installed)

```powershell
.\.venv\Scripts\python scripts\make_tls.py      # once per PC: this PC's own CA + server certificate
.\scripts\run_shop_server.ps1                   # this PC (+ the Android emulator)
.\scripts\run_shop_server.ps1 -Lan              # phones on the shop Wi-Fi
```

`-Lan` puts only the shop network's address (the adapter with a default gateway — not WSL/VirtualBox adapters) into
the connect QR, and needs a Windows Firewall rule for port 8643 on the **Private** profile; the script header has the
exact command to run yourself as administrator (it is not changed automatically). The web pages also work at
`https://<address>:8643`. If the PC's address changes: `make_tls.py --ip <new address>`, restart, and customers
re-scan the connect QR (the CA — and therefore the pin — stays the same).

### 12.3 Build and install the app (one APK for every shop)

```powershell
cd android
.\gradlew.bat assembleDebug testDebugUnitTest   # needs Android SDK 36 and JDK 17 (both on this PC)
```

APK: `android\app\build\outputs\apk\debug\app-debug.apk` (a copy: `dist\IFCreditsWallet-0.2.0-pilot.apk`).
Install with `adb install -r dist\IFCreditsWallet-0.2.0-pilot.apk` (USB debugging), or copy it to the phone and open it.
It is a **debug-signed pilot build**, not a store release.

### 12.4 Use

1. At the shop: open the app → **Scan shop connect QR** (shop screen → *Customer app* → *Show connect QR*).
   The app checks the shop answers and shows **"Pilot Shop added."**
2. **Sign in to Pilot Shop** with the test account the shop gave you.
3. **Scan QR** at checkout → check shop and amount → **Confirm and pay** → receipt.
4. *Change shop* lists all paired shops (open or remove).

### 12.5 Tests and what was verified

- `android\gradlew.bat testDebugUnitTest`: **12 tests** — offline (HTTPS-only addresses, payment-code cleanup, JSON,
  connect-code parsing and refusal of damaged/forged codes) and **live** against the running HTTPS shop server:
  pairing with the shop's real connect code (skipping a dead address first), refusal of a different shop id and of a
  wrong pin, pay flow with the same receipt for shop and customer and no double charge, insufficient balance, amount
  mismatch, tampered code, shop staff and wrong passwords refused, sign-out. Live tests are skipped when the server
  is not running.
- Python: `tests/test_pairing.py` (connect code contents and pin, pin kept when the certificate is renewed,
  staff-only, HTTPS required, shop-network address selection, public shop identity).
- On the Android emulator (2026-10-13): fresh install showing *Choose your shop*, adding Pilot Shop from its connect
  code, *Sign in to Pilot Shop*, wallet showing the shop name and history; earlier: payment 100.00 → 88.00 with receipt
  and the camera scanner opening.
- **Not yet verified:** scanning real QR codes with a real phone camera, and a physical phone over the shop Wi-Fi.

## 13. Security scan (HawkScan)

HawkScan (StackHawk) is an automated attacker: it sends thousands of malicious requests to the running server
(injection, cross-site scripting, broken sign-in/authorization, missing security headers, information leaks) and
reports what gets through. Results also appear at <https://app.stackhawk.com> (application `intraforge-pay-pilot`).

**Never scan the demo server.** A scan writes thousands of junk audit entries, and the audit log is permanent. Scan the
separate scan instance: database `ifpay_scan`, secrets `secrets\scan`, port 8644.

One-time setup:

1. A StackHawk account; put its API key in `secrets\hawk_api_key` (git-ignored; never in `stackhawk.yml`).
2. The `hawk` CLI 6.x — on this PC `C:\Users\Hp\tools\hawk\hawk.exe` (official download, SHA-256 checked against
   StackHawk's manifest). Other location: set `HAWK_EXE`.
3. The scan instance's database and test accounts:

```powershell
$pw = ((Get-Content .env | Where-Object { $_ -like 'IFPAY_DB_PASSWORD=*' }) -split '=',2)[1]
$env:IFPAY_DATABASE_URL = "postgresql://ifpay:$pw@127.0.0.1:55432/ifpay_scan"   # create the database first if needed
$env:IFPAY_SECRET_DIR = "$PWD\secrets\scan"
.\.venv\Scripts\python scripts\seed_pilot.py --allocate
.\.venv\Scripts\python scripts\make_tls.py
```

Each time:

```powershell
# window 1 -- the scan instance (same $env: lines as above, plus):
$env:IFPAY_COOKIE_SECURE = "true"; $env:IFPAY_PUBLIC_PORT = "8644"
.\.venv\Scripts\python -m uvicorn ifpay.web.app:app --host 127.0.0.1 --port 8644 `
    --ssl-keyfile secrets\scan\tls\server.key --ssl-certfile secrets\scan\tls\server.crt
# window 2:
.\.venv\Scripts\python scripts\run_hawkscan.py validate   # config + API description + sign-in
.\.venv\Scripts\python scripts\run_hawkscan.py scan       # the full scan (several minutes)
```

`run_hawkscan.py` regenerates `stackhawk-openapi.json` from the app's routes, signs in to the scan instance as test
customer `customer4`, and hands the session cookie and CSRF token to HawkScan through environment variables. The scan
runs as a customer (non-privileged); shop and admin endpoints are still requested and must answer 403.

Found while setting this up (Windows): HawkScan's `externalCommand` login mangled double quotes passed to `sh` and
then did not apply the cookie it returned, so the scan uses HawkScan's `external` credentials instead (documented to
apply to every request, including the sign-in check).
