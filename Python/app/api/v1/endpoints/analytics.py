"""Document analytics: volume, classification mix, failures, cost and latency.

The dashboard answers "what is happening right now". This answers "where is the
pipeline actually spending time, and what is it getting wrong" - the questions
that come up when quality or spend is disappointing and you need to know which
stage to look at.

Scope follows the caller's role, exactly as the document list does: an
administrator sees the whole installation, everyone else sees their own uploads.
It is derived from the role, never from a parameter, so a normal user cannot ask
for anyone else's figures.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Query, Response

from app.api.deps import CurrentUser, DBSession
from app.repositories import analytics as analytics_repo
from app.schemas.analytics import (
    AnalyticsTotals,
    BucketStat,
    DayStat,
    DocumentAnalyticsResponse,
    FailureStat,
    PerformanceStat,
    ProviderStat,
    TokenStat,
    TypeStat,
    UploaderStat,
)
from app.schemas.common import ErrorResponse

router = APIRouter(
    prefix="/analytics",
    tags=["Analytics"],
    responses={401: {"model": ErrorResponse, "description": "Not authenticated"}},
)

DEFAULT_WINDOW_DAYS = 30
TOP_UPLOADERS = 5

# Longer than the dashboard's 15s: these are heavier aggregates and nobody makes
# a decision on a 30-day trend that turns over in seconds.
_CACHE_CONTROL = "private, max-age=60"

UNCLASSIFIED = "unclassified"


def _share(part: int, whole: int) -> float:
    """Percentage of ``whole``, or 0.0 when there is nothing to divide by."""
    if whole <= 0:
        return 0.0
    return round(part / whole * 100, 1)


@router.get(
    "/documents",
    response_model=DocumentAnalyticsResponse,
    summary="Document processing analytics",
)
async def document_analytics(
    db: DBSession,
    current_user: CurrentUser,
    response: Response,
    window_days: Annotated[
        int, Query(ge=1, le=365, description="Trailing window to report on.")
    ] = DEFAULT_WINDOW_DAYS,
) -> DocumentAnalyticsResponse:
    """Everything measurable about the documents processed in a trailing window.

    * **totals** - volume, outcome, and how much work was repeated.
    * **by_type** - what kind of documents arrive, and how confident the
      analyser was about each class.
    * **failures** - grouped by error code, most common first. Usually the panel
      worth acting on.
    * **performance** - where pipeline time goes, split between text extraction
      and analysis, with p50/p95 rather than a mean that one 40-page scan can
      dominate.
    * **providers** - which engine actually ran. The AI layer falls back to the
      offline engines silently by design, so this is how you find out that it did.
    * **tokens** - prompt and completion totals; the nearest thing to a spend
      figure this service records.
    * **confidence** - how much output is weak enough to want a human.
    * **top_uploaders** - admin only; empty otherwise, where the only possible
      entry is the caller.
    * **daily** - throughput per day across the window.
    """
    response.headers["Cache-Control"] = _CACHE_CONTROL

    now = datetime.now(UTC)
    start = now - timedelta(days=window_days)
    is_admin = current_user.is_admin
    owner_id = None if is_admin else current_user.id

    data = await analytics_repo.collect(
        db,
        start=start,
        end=now,
        owner_id=owner_id,
        top_uploaders=TOP_UPLOADERS,
        include_uploaders=is_admin,
    )

    totals = data.totals
    finished = totals.completed + totals.failed

    # Per-stage denominators: a provider's share is a share of its own stage, not
    # of every extraction row, or text-extraction and analysis would each read as
    # half the real figure.
    stage_totals: dict[str, int] = {}
    for usage in data.providers:
        stage_totals[usage.stage] = stage_totals.get(usage.stage, 0) + usage.documents

    extraction_total = sum(bucket.documents for bucket in data.confidence)

    return DocumentAnalyticsResponse(
        window_days=window_days,
        generated_at=now,
        scope="installation" if is_admin else "own",
        totals=AnalyticsTotals(
            documents=totals.documents,
            completed=totals.completed,
            failed=totals.failed,
            in_progress=totals.in_progress,
            success_rate=_share(totals.completed, finished),
            pages=totals.pages,
            size_bytes=totals.size_bytes,
            reprocessed=totals.reprocessed,
        ),
        by_type=[
            TypeStat(
                document_type=entry.document_type or UNCLASSIFIED,
                documents=entry.documents,
                share=_share(entry.documents, totals.documents),
                failed=entry.failed,
                avg_confidence=(
                    round(entry.avg_confidence, 3)
                    if entry.avg_confidence is not None
                    else None
                ),
                avg_pages=(
                    round(entry.avg_pages, 1) if entry.avg_pages is not None else None
                ),
            )
            for entry in data.by_type
        ],
        failures=[
            FailureStat(
                code=entry.code,
                documents=entry.documents,
                # Share of failures, not of all documents: "60% of failures are
                # no_text_layer" is the actionable framing.
                share=_share(entry.documents, totals.failed),
                example_message=entry.example_message,
                latest_at=entry.latest_at,
            )
            for entry in data.failures
        ],
        performance=PerformanceStat(
            samples=data.performance.samples if data.performance else 0,
            avg_ocr_ms=_round(data.performance.avg_ocr_ms if data.performance else None),
            avg_analysis_ms=_round(
                data.performance.avg_analysis_ms if data.performance else None
            ),
            avg_total_ms=_round(
                data.performance.avg_total_ms if data.performance else None
            ),
            p50_total_ms=data.performance.p50_total_ms if data.performance else None,
            p95_total_ms=data.performance.p95_total_ms if data.performance else None,
            slowest_total_ms=(
                data.performance.slowest_total_ms if data.performance else None
            ),
            avg_ms_per_page=_round(
                data.performance.avg_ms_per_page if data.performance else None
            ),
        ),
        providers=[
            ProviderStat(
                stage=usage.stage,
                provider=usage.provider,
                documents=usage.documents,
                share=_share(usage.documents, stage_totals.get(usage.stage, 0)),
            )
            for usage in data.providers
        ],
        tokens=TokenStat(
            prompt_tokens=data.tokens.prompt_tokens if data.tokens else 0,
            completion_tokens=data.tokens.completion_tokens if data.tokens else 0,
            total_tokens=(
                (data.tokens.prompt_tokens + data.tokens.completion_tokens)
                if data.tokens
                else 0
            ),
            documents_with_tokens=(
                data.tokens.documents_with_tokens if data.tokens else 0
            ),
        ),
        confidence=[
            BucketStat(
                label=bucket.label,
                documents=bucket.documents,
                share=_share(bucket.documents, extraction_total),
            )
            for bucket in data.confidence
        ],
        top_uploaders=[
            UploaderStat(
                id=entry.owner_id,
                email=entry.email,
                full_name=entry.full_name,
                documents=entry.documents,
                failed=entry.failed,
            )
            for entry in data.uploaders
        ],
        daily=[
            DayStat(
                date=point.date,
                documents=point.documents,
                completed=point.completed,
                failed=point.failed,
            )
            for point in data.daily
        ],
    )


def _round(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None
