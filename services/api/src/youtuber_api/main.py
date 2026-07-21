from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from minio import Minio
from opentelemetry import trace
from sqlalchemy import text
from starlette.middleware.trustedhost import TrustedHostMiddleware
from temporalio.client import Client

from youtuber_api.config import get_settings
from youtuber_api.db import SessionFactory
from youtuber_api.metrics import metrics_response, monotonic, observe_request
from youtuber_api.structured_logging import bind_correlation, request_logger, reset_correlation
from youtuber_api.telemetry import configure_telemetry
from youtuber_api.routers import (
    audit,
    auth,
    configuration,
    editorial,
    editorial_config,
    media,
    oidc,
    optimization,
    publishing,
    profiles,
    research,
    system,
    users,
)

settings = get_settings()
_CORRELATION_PATTERN = re.compile(r"^[a-zA-Z0-9_.:-]{1,160}$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.temporal_client = await Client.connect(
        settings.temporal_address, namespace=settings.temporal_namespace
    )
    app.state.minio_client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )
    bucket_exists = await asyncio.to_thread(
        app.state.minio_client.bucket_exists, settings.minio_bucket
    )
    if not bucket_exists:
        await asyncio.to_thread(app.state.minio_client.make_bucket, settings.minio_bucket)
    yield


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    docs_url=None if settings.app_environment == "production" else "/api/docs",
    openapi_url=None if settings.app_environment == "production" else "/api/openapi.json",
    lifespan=lifespan,
)

if settings.app_environment == "production":
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)


@app.middleware("http")
async def correlation_middleware(request: Request, call_next):
    started = monotonic()
    proposed = request.headers.get("X-Correlation-ID", "")
    correlation_id = proposed if _CORRELATION_PATTERN.fullmatch(proposed) else str(uuid4())
    request.state.correlation_id = correlation_id
    correlation_token = bind_correlation(correlation_id)
    try:
        response = await call_next(request)
        route = request.scope.get("route")
        route_path = getattr(route, "path", "unmatched")
        if route_path != "/internal/metrics":
            duration = monotonic() - started
            observe_request(request.method, route_path, response.status_code, duration)
            span = trace.get_current_span()
            if span.is_recording():
                span.set_attribute("app.correlation_id", correlation_id)
            request_logger().info(
                "request_completed",
                extra={"event_fields": {"method": request.method, "route": route_path, "status": response.status_code, "duration_ms": round(duration * 1000, 3)}},
            )
        response.headers["X-Correlation-ID"] = correlation_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        return response
    finally:
        reset_correlation(correlation_token)


app.add_api_route("/internal/metrics", metrics_response, methods=["GET"], include_in_schema=False)


@app.get("/api/health/live", tags=["health"])
async def live() -> dict[str, str]:
    return {"status": "live"}


@app.get("/api/health/ready", tags=["health"])
async def ready(request: Request):
    checks: dict[str, str] = {}
    try:
        async with SessionFactory() as session:
            await session.execute(text("SELECT 1"))
        checks["postgres"] = "ready"
    except Exception as exc:
        checks["postgres"] = f"unavailable:{type(exc).__name__}"
    try:
        exists = await asyncio.to_thread(
            request.app.state.minio_client.bucket_exists, settings.minio_bucket
        )
        checks["object_storage"] = "ready" if exists else "bucket_missing"
    except Exception as exc:
        checks["object_storage"] = f"unavailable:{type(exc).__name__}"
    try:
        await request.app.state.temporal_client.service_client.check_health()
        checks["temporal"] = "ready"
    except Exception as exc:
        checks["temporal"] = f"unavailable:{type(exc).__name__}"
    healthy = all(value == "ready" for value in checks.values())
    return JSONResponse(
        {"status": "ready" if healthy else "not_ready", "checks": checks},
        status_code=200 if healthy else 503,
    )


app.include_router(auth.router, prefix="/api/v1")
app.include_router(oidc.router, prefix="/api/v1")
app.include_router(users.router, prefix="/api/v1")
app.include_router(configuration.router, prefix="/api/v1")
app.include_router(editorial_config.router, prefix="/api/v1")
app.include_router(editorial.router, prefix="/api/v1")
app.include_router(media.router, prefix="/api/v1")
app.include_router(publishing.router, prefix="/api/v1")
app.include_router(optimization.router, prefix="/api/v1")
app.include_router(audit.router, prefix="/api/v1")
app.include_router(system.router, prefix="/api/v1")
app.include_router(profiles.router, prefix="/api/v1")
app.include_router(research.router, prefix="/api/v1")
configure_telemetry(app)
