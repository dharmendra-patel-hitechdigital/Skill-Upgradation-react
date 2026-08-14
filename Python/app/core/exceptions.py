"""Domain exception hierarchy and the HTTP error envelope.

Service and repository code raises *domain* errors - it never imports
``fastapi.HTTPException``. A single set of handlers registered in
:mod:`app.main` translates those into HTTP responses. Benefits:

* the service layer stays reusable from a CLI, a worker, or a test;
* every error response has an identical shape, so clients can parse one thing;
* the HTTP status for a given domain failure is decided in exactly one place.
"""
from __future__ import annotations

from typing import Any

from fastapi import status


class AppError(Exception):
    """Base class for every expected (non-bug) failure in the application."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "internal_error"
    message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message or type(self).message
        self.code = code or type(self).code
        self.details = details or {}
        self.headers = headers or {}
        super().__init__(self.message)


# ------------------------------------------------------------------ 4xx: client
class BadRequestError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "bad_request"
    message = "The request could not be processed."


class AuthenticationError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "not_authenticated"
    message = "Could not validate credentials."

    def __init__(self, message: str | None = None, **kwargs: Any) -> None:
        headers = {"WWW-Authenticate": "Bearer"} | kwargs.pop("headers", {})
        super().__init__(message, headers=headers, **kwargs)


class PermissionDeniedError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "permission_denied"
    message = "You do not have permission to perform this action."


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"
    message = "The requested resource was not found."


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"
    message = "The request conflicts with the current state of the resource."


class UnsupportedMediaTypeError(AppError):
    status_code = status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    code = "unsupported_media_type"
    message = "The uploaded file type is not supported."


class PayloadTooLargeError(AppError):
    status_code = status.HTTP_413_CONTENT_TOO_LARGE
    code = "payload_too_large"
    message = "The uploaded file is too large."


class ValidationError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "validation_error"
    message = "The submitted data is invalid."


class RateLimitedError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"
    message = "Too many requests. Please slow down."


# ---------------------------------------------------- 4xx/5xx: domain specific
class DocumentNotReadyError(ConflictError):
    code = "document_not_ready"
    message = "The document has not finished processing yet."


class StorageError(AppError):
    code = "storage_error"
    message = "The document could not be stored or retrieved."


class ExtractionError(AppError):
    """Raised when the OCR / text-layer stage cannot produce usable text."""

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "extraction_failed"
    message = "No readable text could be extracted from the document."


class AIProviderError(AppError):
    """An upstream AI provider failed (network, quota, malformed response)."""

    status_code = status.HTTP_502_BAD_GATEWAY
    code = "ai_provider_error"
    message = "The AI provider could not process this request."


class AIProviderUnavailableError(AIProviderError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "ai_provider_unavailable"
    message = "No AI provider is configured for this operation."


def error_payload(
    *,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Build the single error envelope used by every non-2xx response."""
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    if request_id:
        body["error"]["request_id"] = request_id
    return body
