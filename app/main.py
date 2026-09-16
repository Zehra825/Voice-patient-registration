"""FastAPI application: wires routers, error handling, logging, dashboard."""

from __future__ import annotations

import hmac
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import database
from app.api.patients import calls_router
from app.api.patients import router as patients_router
from app.api.responses import AppError, fail, ok
from app.config import get_settings
from app.voice.webhook import router as vapi_router

STATIC_DIR = Path(__file__).parent / "static"


def configure_logging() -> None:
    logging.basicConfig(
        stream=sys.stdout,
        level=get_settings().log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        force=True,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    database.init_db()
    if get_settings().seed_demo_data:
        from app.seed import seed_if_empty

        with database.SessionLocal() as db:
            seed_if_empty(db)
    from app.voice import autosetup

    autosetup.start(get_settings())
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        description="Voice AI patient registration: Vapi phone agent + REST API + dashboard.",
        lifespan=lifespan,
    )

    # ---- error handling: every error uses the same envelope -----------------
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        return fail(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError):
        errors = exc.errors()
        if any(e.get("type") == "json_invalid" for e in errors) or any(
            tuple(e.get("loc", ())) == ("body",) and e.get("type") in {"missing", "dict_type"} for e in errors
        ):
            return fail(400, "BAD_REQUEST", "Request body must be a valid JSON object.")
        details = [
            {"field": ".".join(str(p) for p in e.get("loc", ()) if p not in ("body", "query", "path")),
             "message": str(e.get("msg", "")).removeprefix("Value error, ")}
            for e in errors
        ]
        return fail(422, "VALIDATION_ERROR", "One or more fields are invalid.", details)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException):
        codes = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED", 401: "UNAUTHORIZED", 400: "BAD_REQUEST"}
        return fail(exc.status_code, codes.get(exc.status_code, "HTTP_ERROR"), str(exc.detail))

    @app.exception_handler(SQLAlchemyError)
    async def _db_error(_: Request, exc: SQLAlchemyError):
        logging.getLogger("app").exception("database error: %s", exc)
        return fail(500, "DATABASE_ERROR", "A database error occurred. Please try again.")

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        logging.getLogger("app").exception("unhandled error: %s", exc)
        return fail(500, "INTERNAL_ERROR", "An unexpected error occurred.")

    # ---- routes ---------------------------------------------------------------
    app.include_router(patients_router)
    app.include_router(calls_router)
    app.include_router(vapi_router)

    @app.get("/", tags=["meta"], summary="Service info")
    def root():
        from app.voice import autosetup

        s = get_settings()
        return ok({
            "service": s.app_name,
            "clinic": s.clinic_name,
            "phone_number": s.vapi_phone_number or autosetup.snapshot().get("phone_number"),
            "voice_agent": autosetup.snapshot().get("state"),
            "dashboard": "/dashboard",
            "docs": "/docs",
            "endpoints": ["GET /patients", "GET /patients/{id}", "POST /patients", "PUT /patients/{id}", "DELETE /patients/{id}"],
        })

    @app.get("/vapi/status", tags=["voice"], summary="Voice agent setup status and phone number")
    def vapi_status():
        from app.voice import autosetup

        status = autosetup.snapshot()
        if get_settings().vapi_phone_number:
            status["phone_number"] = get_settings().vapi_phone_number
        return ok(status)

    @app.get("/health", tags=["meta"], summary="Liveness + database check")
    def health():
        try:
            with database.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except SQLAlchemyError:
            return fail(503, "DATABASE_UNAVAILABLE", "Database is not reachable.")
        return ok({"status": "ok", "database": database.engine.url.get_backend_name()})

    @app.get("/dashboard", include_in_schema=False)
    def dashboard():
        return FileResponse(STATIC_DIR / "dashboard.html", media_type="text/html")

    def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
        expected = get_settings().admin_token
        if not expected:
            raise AppError(404, "NOT_FOUND", "Not found.")
        if not x_admin_token or not hmac.compare_digest(x_admin_token, expected):
            raise AppError(401, "UNAUTHORIZED", "Invalid admin token.")

    @app.post("/admin/vapi/sync", tags=["admin"], dependencies=[Depends(require_admin)],
              summary="Create/update the Vapi assistant and phone number from code")
    def vapi_sync(area_code: str | None = None, with_phone: bool = True):
        from app.voice import provisioning

        try:
            return ok(provisioning.sync(get_settings(), area_code=area_code, with_phone=with_phone))
        except (provisioning.VapiError, ValueError) as exc:
            raise AppError(502, "VAPI_ERROR", str(exc)) from None

    return app


app = create_app()
