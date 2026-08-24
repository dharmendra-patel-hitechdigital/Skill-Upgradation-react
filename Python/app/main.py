"""FastAPI application factory, error handling, and lifespan management.

Everything global lives here and nowhere else: middleware order, the exception
handlers that produce the one error envelope, the OpenAPI metadata, and the
startup/shutdown sequence.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

# Importing the models package registers every table on Base.metadata.
import app.models
from app.api.v1.router import api_router
from app.core.config import settings
from app.core.database import Base, dispose_engine, engine, session_scope
from app.core.exceptions import AppError, error_payload
from app.core.logging import configure_logging, get_request_id
from app.core.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from app.services.document_processor import recover_stuck_documents
from app.services.task_runner import task_runner

logger = logging.getLogger(__name__)

DESCRIPTION = """
A production-shaped FastAPI backend for **AI-powered document processing**.

Upload a PDF, scan, or photo; the service extracts its text, classifies it,
summarises it, pulls out structured fields and entities, and lets you ask
questions about it in natural language.

### How the document flow works

1. `POST /api/v1/documents` - upload a file. Returns **202 Accepted** straight
   away with `status: "pending"`. Extraction and analysis take seconds to
   minutes, so the request does not block.
2. Poll `GET /api/v1/documents/{id}` until `status` is `completed` or `failed`.
3. A completed document carries the full `extraction`: summary, document type,
   entities, key/value fields, confidence, and provider provenance.
4. `POST /api/v1/documents/{id}/ask` answers questions grounded in that
   document's text - with an explicit `answer_found` flag when the answer simply
   is not there.

### Authentication

1. `POST /api/v1/auth/register` - create an account (the first account on a
   fresh install becomes an admin).
2. `POST /api/v1/auth/login` - OAuth2 password form; put the **email** in the
   `username` field.
3. Send `Authorization: Bearer <access_token>` on every other call, or click
   **Authorize** above to do it automatically in this page.
4. Access tokens are short-lived. `POST /api/v1/auth/refresh` rotates them -
   refresh tokens are **single-use**, so store each new one.

### AI providers

The AI layer sits behind protocols with graceful degradation, so the feature
works with no third-party credentials at all:

| Stage | Preferred | Built-in fallback |
|---|---|---|
| Text extraction | AWS Textract (scans, photos, multi-page) | `pypdf` text layer + plain text |
| Analysis | OpenAI structured outputs | rule-based analyser |

Call `GET /api/v1/health/providers` to see which engines are actually active.

### Errors

Every non-2xx response uses one envelope:

```json
{"error": {"code": "not_found", "message": "Document 42 was not found.",
           "request_id": "9f2c1ab34de5f678"}}
```

`request_id` is also returned as the `X-Request-ID` header and appears in the
server logs - quote it when reporting a problem.
"""

TAGS_METADATA: list[dict[str, Any]] = [
    {
        "name": "Health",
        "description": "Liveness, readiness, and AI provider introspection.",
    },
    {
        "name": "Authentication",
        "description": (
            "Registration, login, single-use refresh-token rotation, and logout."
        ),
    },
    {
        "name": "Users",
        "description": "Profile management, password changes, and admin user management.",
    },
    {
        "name": "Documents",
        "description": (
            "Upload documents, retrieve AI extraction results, ask grounded "
            "questions, and reprocess."
        ),
    },
    {
        "name": "Analytics",
        "description": (
            "Aggregated document-processing analytics: classification mix, "
            "failure breakdown, pipeline latency, provider usage and token "
            "totals. Scoped to the caller's own documents, or the whole "
            "installation for an admin."
        ),
    },
    {
        "name": "Dashboard",
        "description": (
            "Aggregated summary panels: headline stats, a monthly volume "
            "series, and the recent-activity feed. Scoped to the caller's own "
            "documents, or the whole installation for an admin."
        ),
    },
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup and shutdown sequence."""
    configure_logging()
    logger.info(
        "application_starting",
        extra={
            "environment": settings.ENVIRONMENT.value,
            "version": settings.VERSION,
            "database": engine.url.render_as_string(hide_password=True),
        },
    )

    if settings.should_auto_create_tables:
        # Convenience for local development and tests only. Production uses
        # Alembic - see `alembic upgrade head` in the README. `create_all` cannot
        # express an ALTER, so it silently diverges from the models over time.
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        logger.info("schema_ensured_via_create_all")

    if settings.STORAGE_BACKEND == "local":
        settings.STORAGE_LOCAL_DIR.mkdir(parents=True, exist_ok=True)

    task_runner.reopen()

    try:
        from app.repositories import refresh_token as token_repo
        from app.services.auth_service import bootstrap_first_admin

        async with session_scope() as db:
            await bootstrap_first_admin(db)
            pruned = await token_repo.delete_expired(db)
            if pruned:
                logger.info("pruned_expired_refresh_tokens", extra={"count": pruned})

        # Documents left mid-flight by a previous process cannot resume, so return
        # them to a retryable state instead of leaving them stuck.
        await recover_stuck_documents()
    except Exception:
        # A failed housekeeping step must not stop the app from serving traffic.
        logger.exception("startup_housekeeping_failed")

    from app.services.ai.registry import describe_providers

    providers = describe_providers()
    logger.info(
        "ai_providers_ready",
        extra={
            "text_extraction": providers.text_extraction,
            "analysis": providers.analysis,
        },
    )
    for note in providers.notes:
        logger.warning("ai_provider_note", extra={"note": note})

    yield

    logger.info("application_stopping")
    await task_runner.drain()
    await dispose_engine()


def create_app() -> FastAPI:
    """Build and configure the application.

    A factory (rather than module-level construction) so tests can build an app
    per configuration, and so ``uvicorn --factory`` works.
    """
    app = FastAPI(
        title=settings.PROJECT_NAME,
        version=settings.VERSION,
        description=DESCRIPTION,
        summary="AI document processing with JWT-secured, async FastAPI.",
        openapi_tags=TAGS_METADATA,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        # Hide the noisy 422 default; each route documents its own error responses.
        responses={},
        contact={"name": "API support", "email": "support@example.com"},
        license_info={"name": "MIT"},
        swagger_ui_parameters={
            "persistAuthorization": True,  # survive a page reload while testing
            "displayRequestDuration": True,
            "docExpansion": "none",
            "filter": True,
            "tryItOutEnabled": True,
        },
    )

    _register_middleware(app)
    _register_exception_handlers(app)

    app.include_router(api_router, prefix=settings.API_V1_PREFIX)

    @app.get("/", tags=["Health"], summary="Service banner")
    async def root() -> dict[str, str]:
        """Human-friendly entry point that points at the docs."""
        return {
            "service": settings.PROJECT_NAME,
            "version": settings.VERSION,
            "environment": settings.ENVIRONMENT.value,
            "docs": "/docs",
            "openapi": "/openapi.json",
            "api": settings.API_V1_PREFIX,
        }

    return app


def _register_middleware(app: FastAPI) -> None:
    """Install middleware.

    Order matters: Starlette applies middleware bottom-up, so the last one added
    is the outermost. Request-context is added last and therefore wraps
    everything - meaning even a CORS rejection gets a request id and an access log
    line.
    """
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        # Without this the browser cannot read these headers from JS, which
        # breaks correlating a client-side error with a server log.
        expose_headers=["X-Request-ID", "X-Process-Time-Ms"],
        max_age=600,
    )
    app.add_middleware(RequestContextMiddleware)


def _register_exception_handlers(app: FastAPI) -> None:
    """Translate every exception into the single error envelope."""

    @app.exception_handler(AppError)
    async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        """Domain errors already carry their status, code, and safe message."""
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(
                code=exc.code,
                message=exc.message,
                details=exc.details,
                request_id=get_request_id(),
            ),
            headers=exc.headers or None,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Reshape FastAPI's validation errors into the standard envelope."""
        fields = [
            {
                # Drop the leading location segment ("body"/"query") - the client
                # knows where it put the value; it needs the field name.
                "field": ".".join(str(part) for part in error["loc"][1:]) or "request",
                "message": error.get("msg", "Invalid value."),
                "type": error.get("type", "value_error"),
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=error_payload(
                code="validation_error",
                message="One or more fields are invalid.",
                details={"fields": fields},
                request_id=get_request_id(),
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        _: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        """Catch framework-raised HTTP errors (404 routing, 405, ...)."""
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(
                code=_HTTP_ERROR_CODES.get(exc.status_code, "http_error"),
                message=str(exc.detail),
                request_id=get_request_id(),
            ),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        """Last resort: log the full traceback, return an opaque 500.

        The message deliberately reveals nothing about internals - stack traces
        and driver errors leak schema and file paths. The request id is the bridge
        between what the user sees and what we logged.
        """
        logger.exception("unhandled_exception", extra={"error_type": type(exc).__name__})
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=error_payload(
                code="internal_error",
                message=(
                    "An unexpected error occurred. Quote the request_id when "
                    "reporting this."
                ),
                request_id=get_request_id(),
            ),
        )


_HTTP_ERROR_CODES: dict[int, str] = {
    status.HTTP_400_BAD_REQUEST: "bad_request",
    status.HTTP_401_UNAUTHORIZED: "not_authenticated",
    status.HTTP_403_FORBIDDEN: "permission_denied",
    status.HTTP_404_NOT_FOUND: "not_found",
    status.HTTP_405_METHOD_NOT_ALLOWED: "method_not_allowed",
    status.HTTP_409_CONFLICT: "conflict",
    status.HTTP_413_CONTENT_TOO_LARGE: "payload_too_large",
    status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: "unsupported_media_type",
    status.HTTP_429_TOO_MANY_REQUESTS: "rate_limited",
    status.HTTP_500_INTERNAL_SERVER_ERROR: "internal_error",
    status.HTTP_503_SERVICE_UNAVAILABLE: "service_unavailable",
}


app = create_app()


def custom_openapi() -> dict[str, Any]:
    """Cache the generated schema and add a few things FastAPI cannot infer."""
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=app.version,
        summary=app.summary,
        description=app.description,
        routes=app.routes,
        tags=TAGS_METADATA,
        contact=app.contact,
        license_info=app.license_info,
    )
    schema["servers"] = [
        {"url": "/", "description": "This server"},
        {"url": "http://127.0.0.1:8000", "description": "Local development"},
    ]
    # Document the bearer format so tooling and client generators emit a real JWT
    # type rather than an opaque string.
    components = schema.setdefault("components", {}).setdefault("securitySchemes", {})
    components["HTTPBearer"] = {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": "Paste the `access_token` returned by /auth/login.",
    }
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi  # type: ignore[method-assign]
