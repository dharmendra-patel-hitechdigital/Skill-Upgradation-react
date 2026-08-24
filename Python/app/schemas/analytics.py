"""Response schemas for the document analytics endpoint.

Shares are pre-computed server-side rather than left to the client. Two clients
would otherwise each pick their own rounding and denominator, and a "share of
failures" that does not sum to 100% is the kind of thing that gets a dashboard
distrusted.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class AnalyticsTotals(BaseModel):
    documents: int
    completed: int
    failed: int
    in_progress: int
    success_rate: float = Field(
        description=(
            "completed / (completed + failed), as a percentage. Documents still "
            "in flight are excluded rather than counted as failures."
        )
    )
    pages: int
    size_bytes: int
    reprocessed: int = Field(
        description="Documents the pipeline ran more than once (a retry or a reprocess)."
    )


class TypeStat(BaseModel):
    document_type: str = Field(
        description="Classification, or `unclassified` when the analyser assigned none."
    )
    documents: int
    share: float = Field(description="Percentage of all documents in the window.")
    failed: int
    avg_confidence: float | None = Field(
        default=None, description="Mean analyser confidence, 0..1."
    )
    avg_pages: float | None = None


class FailureStat(BaseModel):
    code: str = Field(examples=["no_text_layer"])
    documents: int
    share: float = Field(description="Percentage of failures, not of all documents.")
    example_message: str | None = Field(
        default=None,
        description=(
            "One real message from this group - not necessarily the most recent "
            "one. Use the document list to see a specific occurrence."
        ),
    )
    latest_at: datetime | None = None


class PerformanceStat(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "description": (
                "Pipeline time (text extraction + analysis), not end-to-end wall "
                "clock: wall clock includes queue wait behind other uploads."
            )
        }
    )

    samples: int = Field(description="Documents with a recorded extraction.")
    avg_ocr_ms: float | None = None
    avg_analysis_ms: float | None = None
    avg_total_ms: float | None = None
    p50_total_ms: int | None = None
    p95_total_ms: int | None = None
    slowest_total_ms: int | None = None
    avg_ms_per_page: float | None = None


class ProviderStat(BaseModel):
    stage: str = Field(examples=["text_extraction", "analysis"])
    provider: str = Field(examples=["textract", "local", "openai", "heuristic"])
    documents: int
    share: float = Field(description="Percentage within this stage.")


class TokenStat(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    documents_with_tokens: int = Field(
        description=(
            "Documents that reported token usage. Zero means every analysis ran "
            "on the built-in rule engine, which uses no tokens - so the totals "
            "above being zero is correct, not missing data."
        )
    )


class BucketStat(BaseModel):
    label: str
    documents: int
    share: float


class UploaderStat(BaseModel):
    id: int
    email: EmailStr
    full_name: str | None = None
    documents: int
    failed: int


class DayStat(BaseModel):
    date: str = Field(description="ISO date, `YYYY-MM-DD`.", examples=["2026-08-24"])
    documents: int
    completed: int
    failed: int


class DocumentAnalyticsResponse(BaseModel):
    window_days: int
    generated_at: datetime
    scope: str = Field(
        description=(
            "`installation` when an administrator is looking at every user's "
            "documents, `own` when the figures cover only the caller's."
        ),
        examples=["installation", "own"],
    )
    totals: AnalyticsTotals
    by_type: list[TypeStat]
    failures: list[FailureStat]
    performance: PerformanceStat
    providers: list[ProviderStat]
    tokens: TokenStat
    confidence: list[BucketStat]
    top_uploaders: list[UploaderStat] = Field(
        default_factory=list,
        description=(
            "Busiest uploaders. Empty for a non-admin caller, for whom the only "
            "possible entry is themselves."
        ),
    )
    daily: list[DayStat]
