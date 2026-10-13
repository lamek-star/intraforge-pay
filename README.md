# IntraForge Pay — Single-Shop Pilot MVP

**SIMULATED VALUE — NO CASH VALUE.** A working QR-payment pilot for one shop, five test customers and simulated IF
Credits, connected to IntraForge through a developer application credential and its bucket API.

- `ifpay/` — the shop server (FastAPI) and its pages: `/shop` (cashier), `/wallet` (customer), `/admin`
  (IntraForge administrator / supervisor), `/login`.
- `ifpay/migrations/0001_initial.sql` — the PostgreSQL double-entry journal and its database-enforced rules.
- `scripts/` — IntraForge administrator setup, pilot seed data, the 12-step demonstration, demo reset.
- `intraforge-sandbox/` — an isolated IntraForge for the pilot (own Docker project, volumes and port).
- `tests/` — 83 automated tests (79 against real PostgreSQL, 4 live against a real IntraForge).
- `android/` — the customer wallet Android app (Kotlin); see manual section 12.
- **[docs/MANUAL.md](docs/MANUAL.md)** — step-by-step installation and usage manual.

Quick start (after the one-time installation in the manual):

```powershell
docker compose --env-file .env up -d
.\.venv\Scripts\python -m uvicorn ifpay.web.app:app --host 127.0.0.1 --port 8600
# http://localhost:8600 — accounts in secrets\test-accounts.json
```

Third-party code shipped with the pages: jsQR 1.4.0 (Apache-2.0, `ifpay/static/vendor/jsQR.LICENSE`).

Not for production. No real funds, banking, top-ups, payouts or offline payments.
