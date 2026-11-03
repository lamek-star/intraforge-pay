"""The IntraForge Pay web server: JSON API + static pages (login, admin, shop, wallet), served by IntraForge's proxy
on IntraForge's own origin under Pay's launch path (IFPAY_BASE_PATH, e.g. /apps/pay-1)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .. import db, directory, runtime
from ..config import VERSION, get_settings
from ..errors import DomainError
from ..platform import IntegrationError
from . import api_admin, api_auth, api_internal, api_shop, api_wallet
from .deps import current_principal

log = logging.getLogger("ifpay")
STATIC = Path(__file__).resolve().parent.parent / "static"

SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
                                "style-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; "
                                "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(self), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}

INTEGRATION_STATUS = {"not_configured": 503, "not_installed": 503, "disabled": 503, "rejected": 502,
                      "unavailable": 503, "upload_failed": 502}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    runtime.start()
    yield
    runtime.stop()
    db.close_pool()


def create_app() -> FastAPI:
    app = FastAPI(title="IntraForge Pay (simulated value only)", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if "/api/" in request.url.path:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(DomainError)
    async def domain_error(_request: Request, exc: DomainError):
        return JSONResponse({"code": exc.code, "message": exc.message, **getattr(exc, "extra", {})},
                            status_code=exc.status)

    @app.exception_handler(IntegrationError)
    async def integration_error(_request: Request, exc: IntegrationError):
        return JSONResponse({"code": f"intraforge_{exc.code}", "message": exc.message},
                            status_code=INTEGRATION_STATUS.get(exc.code, 502))

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError):
        return JSONResponse({"code": "invalid_request", "message": "The request is not valid."}, status_code=422)

    @app.exception_handler(psycopg.errors.CheckViolation)
    async def check_violation(_request: Request, exc: psycopg.Error):
        # The database refused a write the application should already have refused: never a silent success.
        log.warning("database rule refused a write: %s", exc.diag.message_primary)
        return JSONResponse({"code": "ledger_rule_violation", "message": "The ledger refused this operation."},
                            status_code=409)

    for module in (api_auth, api_shop, api_wallet, api_admin, api_internal):
        app.include_router(module.router)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def page(name: str):
        def handler():
            return FileResponse(STATIC / f"{name}.html", headers={"Cache-Control": "no-store"})

        return handler

    for name in ("login", "admin", "shop", "wallet"):
        app.add_api_route(f"/{name}", page(name), methods=["GET"], include_in_schema=False)

    @app.get("/", include_in_schema=False)
    def root(request: Request):
        """Where "Open" in IntraForge lands: each person's own start page (relative, so the base path stays)."""
        try:
            principal = current_principal(request)
        except DomainError:
            return RedirectResponse("login", status_code=303)
        home = api_auth.home_for(principal.user)
        return RedirectResponse("login?reason=no-access" if home == "no-access" else home, status_code=303)

    @app.get("/api/shop-identity", include_in_schema=False)
    def shop_identity():
        """Public: which shop this server is, so a paired app can confirm it reached the shop it paired with."""
        with db.connection() as conn:
            merchant = directory.the_merchant(conn)
        if merchant is None:
            raise DomainError("no_shop", "This server has no shop yet.", status=503)
        return {"name": merchant["name"], "merchant_id": str(merchant["id"])}

    @app.get("/api/v1/health", include_in_schema=False)
    def health():
        """IntraForge's Apps page probes this (Running / Degraded / Stopped)."""
        if not db.is_open():
            return JSONResponse({"data": {"status": "starting", "store": "not connected", "version": VERSION}},
                                status_code=503)
        with db.connection() as conn:
            conn.execute("SELECT 1")
        return {"data": {"status": "ok", "store": "IntraForge PostgreSQL", "version": VERSION}}

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return health()

    return app


def create_root() -> FastAPI:
    """Pay's pages and API under its base path; everything else on the origin belongs to IntraForge."""
    inner = create_app()
    base = get_settings().base_path
    if not base:
        inner.router.lifespan_context = lifespan
        return inner
    outer = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    outer.mount(base, inner)

    @outer.get(base, include_in_schema=False)
    def slash():
        return RedirectResponse(base + "/", status_code=308)

    return outer


app = create_root()
