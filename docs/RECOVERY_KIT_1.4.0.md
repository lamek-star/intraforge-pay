# IntraForge Pay 1.4.0 recovery (2026-11-03)

IntraForge kit 1.4.0 (2026-10-28) shipped IntraForge Pay built from commit `b8ebedcf1`, which was never pushed; the
build machine is no longer available. This branch, `recovered/kit-1.4.0`, puts back what the shipped image contains.

**This is not the original history.** It is one commit on top of `main` (`8817227`, 2026-10-13). The commits between
`8817227` and `b8ebedcf1` are lost.

## Source

`intraforge-shop-pay:1.4.0-dbf44f593`, image id
`sha256:cbf8ae4d4717b8ac377ee035ce539e645421ac9e42913a56a3c4799bbbed0424`, built 2026-10-28T09:11:08Z. Copied out of
`/srv/ifpay` with `docker create` + `docker cp`; never started. Secret scan of every file: nothing found.

## Recovered

- **`ifpay/`**: the whole package as shipped. New since `8817227`: `platform.py`, `runtime.py`, `keys.py`,
  `web/api_internal.py`, migrations `0002_intraforge_service_app.sql` and `0003_requests_from_other_apps.sql` (Pay
  running inside IntraForge as a service app, and payment requests from Spare Parts and hosted apps). Removed:
  `intraforge.py`.
- **`ifpay/static/download/IFCreditsWallet.apk`** (7.7 MB): the built Android wallet that 1.4.0 serves to phones.
  Its source is **not** recovered; it is kept because it is the only copy of that build.
- `requirements.txt` is unchanged.

## Not recovered

`android/` source changes, `tests/`, `scripts/`, `docs/MANUAL.md` and anything else outside `ifpay/` after
`8817227`. The tests on this branch are the `8817227` tests and were written for the standalone pilot; expect some to
need updating for the service-app version.
