"""Logging setup and request-correlation plumbing.

A ``request_id`` is generated (or taken from an inbound ``X-Request-ID``) for
every request and stashed in a :class:`~contextvars.ContextVar`. Because
contextvars follow the async task, any log line emitted anywhere down the call
stack - including inside a background document-processing job - is
automatically stamped with the id that started it. That is what makes a
multi-stage AI pipeline debuggable in production.
"""
from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from typing import Any

from app.core.config import settings

request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)

# LogRecord attributes that are always present; anything else was passed by the
# caller via `extra=` and therefore belongs in the structured output.
_RESERVED = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName"}


def get_request_id() -> str | None:
    return request_id_ctx.get()


def safe_extra(values: dict[str, Any]) -> dict[str, Any]:
    """Make a dict safe to pass as ``logging``'s ``extra=``.

    ``Logger.makeRecord`` raises ``KeyError`` if ``extra`` contains a key that
    already exists on the ``LogRecord`` - and the reserved set includes tempting
    names like ``filename``, ``module``, ``name``, ``process`` and ``args``. The
    failure only fires when that log line is actually emitted, so it typically
    lurks on an error path and then crashes the error handler itself.

    Any colliding key is prefixed with ``ctx_`` rather than dropped, so no
    diagnostic information is lost. Use this whenever the context dict is built
    dynamically; a literal with known-safe keys does not need it.
    """
    return {
        (f"ctx_{key}" if key in _RESERVED else key): value
        for key, value in values.items()
    }


class RequestIdFilter(logging.Filter):
    """Copy the contextvar onto every record so formatters can use it."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get() or "-"
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line - what log shippers (CloudWatch, Loki) want."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = _safe(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    """Human-friendly single line for local development."""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED and not key.startswith("_") and key != "request_id"
        }
        if extras:
            rendered = " ".join(f"{k}={_safe(v)}" for k, v in extras.items())
            return f"{base} | {rendered}"
        return base


def _safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    return str(value)


def configure_logging() -> None:
    """Install handlers/formatters. Idempotent - safe to call more than once."""
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(
        JsonFormatter()
        if settings.LOG_JSON
        else ConsoleFormatter(
            fmt="%(asctime)s %(levelname)-8s [%(request_id)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
    )

    root = logging.getLogger()
    for existing in root.handlers[:]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(settings.LOG_LEVEL)

    # uvicorn installs its own handlers; let ours own the output instead so
    # every line - app and server - carries the request id.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        server_logger = logging.getLogger(name)
        server_logger.handlers = []
        server_logger.propagate = True

    # RequestContextMiddleware already emits a richer access line (with the
    # request id, duration and client). Leaving uvicorn's own access log at INFO
    # would print a second, poorer line for every single request.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    # SQLAlchemy is chatty at INFO when echo is on; keep it opt-in.
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.DB_ECHO else logging.WARNING
    )
    # The OpenAI SDK logs full request bodies at DEBUG - never useful here and
    # a PII risk since document text is in the payload.
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
