"""ASGI middleware: request correlation, access logs, and security headers."""
from __future__ import annotations

import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.core.logging import request_id_ctx

logger = logging.getLogger("app.access")

REQUEST_ID_HEADER = "X-Request-ID"
PROCESS_TIME_HEADER = "X-Process-Time-Ms"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign a request id, time the request, and emit one structured log line.

    The id is echoed back on the response so a user reporting a failure can
    quote it and we can find the exact trace - including the AI provider calls
    made while serving them.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex[:16]
        token = request_id_ctx.set(request_id)
        request.state.request_id = request_id

        started = time.perf_counter()
        try:
            response = await call_next(request)

            elapsed_ms = (time.perf_counter() - started) * 1000
            response.headers[REQUEST_ID_HEADER] = request_id
            response.headers[PROCESS_TIME_HEADER] = f"{elapsed_ms:.2f}"

            # Logged inside the `try`, not after it: the `finally` below resets
            # the contextvar, and anything logged past that point would carry no
            # request id - defeating the entire purpose of this middleware.
            logger.info(
                "request_completed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": round(elapsed_ms, 2),
                    "client": request.client.host if request.client else None,
                },
            )
            return response
        except Exception:
            elapsed_ms = (time.perf_counter() - started) * 1000
            # The exception handlers turn this into a response body; here we
            # only guarantee the failure is logged with its timing and route.
            logger.exception(
                "request_failed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": round(elapsed_ms, 2),
                },
            )
            raise
        finally:
            request_id_ctx.reset(token)


class SecurityHeadersMiddleware:
    """Attach conservative security headers to every response.

    Implemented as raw ASGI (rather than BaseHTTPMiddleware) so it adds no
    per-request task overhead and never interferes with streaming responses -
    which matters for the document-download endpoint.
    """

    _HEADERS: tuple[tuple[bytes, bytes], ...] = (
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"referrer-policy", b"no-referrer"),
        (b"cross-origin-opener-policy", b"same-origin"),
    )

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message) -> None:  # type: ignore[no-untyped-def]
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                present = {name.lower() for name, _ in headers}
                headers.extend(
                    (name, value)
                    for name, value in self._HEADERS
                    if name not in present
                )
            await send(message)

        await self.app(scope, receive, send_with_headers)
