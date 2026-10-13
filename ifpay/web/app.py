"""The IntraForge Pay pilot web server: JSON API + four static pages (login, admin, shop, wallet)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .. import db, directory
from ..config import get_settings
from ..errors import DomainError
from ..intraforge import IntegrationError
from . import api_admin, api_auth, api_shop, api_wallet

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

INTEGRATION_STATUS = {"not_configured": 503, "credential_missing": 503, "credential_rejected": 502,
                      "not_authorized": 502, "not_found": 502, "wrong_application": 502, "unavailable": 503,
                      "upload_failed": 502}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    applied = db.migrate(settings.database_url)
    if applied:
        log.info("applied migrations: %s", ", ".join(applied))
    db.open_pool(settings.database_url)
    yield
    db.close_pool()


def create_app() -> FastAPI:
    app = FastAPI(title="IntraForge Pay — pilot (simulated value only)", lifespan=lifespan, docs_url=None,
                  redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(DomainError)
    async def domain_error(_request: Request, exc: DomainError):
        return JSONResponse({"code": exc.code, "message": exc.message}, status_code=exc.status)

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

    for module in (api_auth, api_shop, api_wallet, api_admin):
        app.include_router(module.router)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def page(name: str):
        def handler():
            return FileResponse(STATIC / f"{name}.html", headers={"Cache-Control": "no-store"})

        return handler

    for name in ("login", "admin", "shop", "wallet"):
        app.add_api_route(f"/{name}", page(name), methods=["GET"], include_in_schema=False)

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/login")

    @app.get("/api/shop-identity", include_in_schema=False)
    def shop_identity():
        """Public: which shop this server is, so a paired app can confirm it reached the shop it paired with."""
        with db.connection() as conn:
            merchant = directory.the_merchant(conn)
        if merchant is None:
            raise DomainError("no_shop", "This server has no shop yet.", status=503)
        return {"name": merchant["name"], "merchant_id": str(merchant["id"])}

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        with db.connection() as conn:
            conn.execute("SELECT 1")
        return {"status": "ok"}

    return app


app = create_app()
