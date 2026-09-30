"""FastAPI application entry point."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.router import api_router
from app.api.web import PAGES, WEB_ROOT, router as web_router
from app.config import get_settings
from app.database import verify_database_connection
from app.exceptions import AppError
from app.logging import configure_logging
from app.operations import verify_redis_connection

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    configure_logging(settings.debug)
    yield


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "Idempotency-Key",
        "X-P2P-Signature",
        "X-White-Label-Signature",
        "X-Telegram-Bot-Api-Secret-Token",
    ],
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if request.url.path.startswith("/api/") or request.url.path in PAGES:
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(AppError)
async def handle_app_error(_: Request, error: AppError) -> JSONResponse:
    return JSONResponse(status_code=error.status_code, content={"code": error.code, "detail": error.message})


@app.get("/health", tags=["operations"])
async def health() -> dict[str, str]:
    """Liveness only. It does not assert a usable database or broker."""
    return {"status": "ok", "service": "e-ticket-platform", "environment": settings.app_env}


@app.get("/ready", tags=["operations"])
async def ready() -> JSONResponse:
    """Report dependency readiness without exposing DSNs or exception text."""

    checks: dict[str, str] = {}
    for name, probe in (
        ("database", verify_database_connection),
        ("redis", verify_redis_connection),
    ):
        try:
            await asyncio.wait_for(probe(), timeout=2.0)
        except Exception:
            checks[name] = "unavailable"
        else:
            checks[name] = "ok"
    healthy = all(value == "ok" for value in checks.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ready" if healthy else "not_ready", "checks": checks},
        headers={"Cache-Control": "no-store"},
    )


app.include_router(api_router)
app.mount("/assets", StaticFiles(directory=WEB_ROOT / "static"), name="assets")
app.include_router(web_router)
