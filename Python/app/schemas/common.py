"""Reusable response envelopes and query-parameter models."""
from __future__ import annotations

import math
from typing import Annotated, Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class Message(BaseModel):
    """Simple acknowledgement body for endpoints with nothing else to return."""

    detail: str = Field(examples=["Operation completed successfully."])


class ErrorDetail(BaseModel):
    code: str = Field(examples=["not_found"])
    message: str = Field(examples=["The requested resource was not found."])
    details: dict[str, Any] | None = None
    request_id: str | None = Field(
        default=None,
        description="Correlation id - quote this when reporting a problem.",
        examples=["9f2c1ab34de5f678"],
    )


class ErrorResponse(BaseModel):
    """The single error shape returned by every non-2xx response."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "error": {
                    "code": "not_found",
                    "message": "Document 42 was not found.",
                    "request_id": "9f2c1ab34de5f678",
                }
            }
        }
    )

    error: ErrorDetail


class PageMeta(BaseModel):
    total: int = Field(description="Total matching records, ignoring pagination.")
    page: int = Field(description="Current 1-based page number.")
    page_size: int
    total_pages: int
    has_next: bool
    has_previous: bool


class Page(BaseModel, Generic[T]):
    """Paginated collection envelope.

    A wrapper (rather than a bare array) so pagination metadata has somewhere to
    live, and so extra top-level keys can be added later without breaking
    clients that already parse ``items``.
    """

    items: list[T]
    meta: PageMeta

    @classmethod
    def build(cls, items: list[T], *, total: int, page: int, page_size: int) -> Page[T]:
        total_pages = max(1, math.ceil(total / page_size)) if page_size else 1
        return cls(
            items=items,
            meta=PageMeta(
                total=total,
                page=page,
                page_size=page_size,
                total_pages=total_pages,
                has_next=page < total_pages,
                has_previous=page > 1,
            ),
        )


class PaginationParams(BaseModel):
    """Shared ``?page=&page_size=`` query parameters.

    ``page_size`` is capped server-side: an unbounded limit is an easy way for a
    client to accidentally (or deliberately) exhaust the database.
    """

    page: Annotated[int, Field(ge=1, description="1-based page number.")] = 1
    page_size: Annotated[
        int, Field(ge=1, le=100, description="Records per page (max 100).")
    ] = 20

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        return self.page_size
