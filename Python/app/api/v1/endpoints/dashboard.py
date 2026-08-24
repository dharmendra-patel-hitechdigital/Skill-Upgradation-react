"""Dashboard summary endpoints: headline stats, a monthly series, and a feed.

Scope rule, applied identically by all three routes: a normal user sees metrics
derived from **their own** documents; an admin sees the whole installation. The
scope comes from the caller's role, never from a query parameter, so there is no
way to ask for someone else's numbers.

These are aggregates over live tables, so they are cheap but not free. Each
route is one round trip (see :mod:`app.repositories.dashboard`) and each carries
a short ``Cache-Control`` window, which is what keeps a dashboard left open on a
wall display from turning into steady database load.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Query, Response

from app.api.deps import CurrentUser, DBSession
from app.models.document import DocumentStatus
from app.models.user import User
from app.repositories import dashboard as dashboard_repo
from app.schemas.common import ErrorResponse
from app.schemas.dashboard import (
    ActivityItem,
    ActivityResponse,
    SeriesMeta,
    SeriesPoint,
    SeriesResponse,
    StatCard,
    StatFormat,
    StatsResponse,
    Trend,
)

router = APIRouter(
    prefix="/dashboard",
    tags=["Dashboard"],
    responses={401: {"model": ErrorResponse, "description": "Not authenticated"}},
)

# How long the browser may reuse a response. Long enough to absorb a rapid
# remount or a double-click on Refresh, short enough that an upload shows up
# essentially immediately.
_CACHE_CONTROL = "private, max-age=15"

DEFAULT_WINDOW_DAYS = 30
DEFAULT_SERIES_MONTHS = 8
DEFAULT_ACTIVITY_LIMIT = 8

# event name -> (phrase, icon/tone key for the client)
_EVENT_PRESENTATION: dict[str, tuple[str, str]] = {
    "uploaded": ("uploaded", "upload"),
    "processing_started": ("started processing", "processing"),
    "processing_completed": ("completed extraction on", "completed"),
    "processing_failed": ("failed to process", "failed"),
    "processing_interrupted": ("was interrupted processing", "failed"),
    "reprocess_requested": ("queued a reprocess of", "reprocess"),
}


def _owner_scope(user: User) -> int | None:
    """``None`` (all users) for an admin, otherwise the caller's own id."""
    return None if user.is_admin else user.id


def _percent_change(current: float, previous: float) -> tuple[float, Trend]:
    """Relative change between two windows, as a signed percentage.

    A previous window of zero has no defined percentage change. Reporting
    ``+100%`` for "something where there was nothing" is the reading a dashboard
    user expects; ``0%`` for "still nothing" avoids implying growth.
    """
    if previous == 0:
        change = 100.0 if current > 0 else 0.0
    else:
        change = (current - previous) / previous * 100
    return round(change, 1), Trend.UP if change >= 0 else Trend.DOWN


def _point_change(current: float, previous: float) -> tuple[float, Trend]:
    """Difference between two percentages, in percentage points.

    A success rate moving 90% -> 95% is "+5 points", not "+5.6%". Conflating the
    two is how dashboards end up reporting impossible improvements.
    """
    change = round(current - previous, 1)
    return change, Trend.UP if change >= 0 else Trend.DOWN


def _data_volume(size_bytes: int) -> tuple[float, str]:
    """Total upload size as ``(value, unit)``, scaled to a readable magnitude.

    A fixed MB unit renders a few hundred kilobytes as "0.0 MB", which on a card
    labelled "Data Processed" is indistinguishable from having processed nothing.
    The delta is computed from raw bytes, so it is unaffected by the unit shown.
    """
    if size_bytes < 1_048_576:
        return round(size_bytes / 1024, 1), "KB"
    if size_bytes < 1_073_741_824:
        return round(size_bytes / 1_048_576, 1), "MB"
    return round(size_bytes / 1_073_741_824, 2), "GB"


def _human_size(size_bytes: int) -> str:
    """Compact byte size for the activity badge (`248 KB`, `1.4 MB`)."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1_048_576:
        return f"{round(size_bytes / 1024)} KB"
    return f"{size_bytes / 1_048_576:.1f} MB"


def _relative_time(moment: datetime, *, now: datetime) -> str:
    """Format an instant as "2 min ago".

    Done server-side because the SPA renders ``item.time`` verbatim. A future
    timestamp (clock skew between the app server and the database) is clamped to
    "just now" rather than rendered as a negative age.
    """
    seconds = (now - moment).total_seconds()
    if seconds < 60:
        return "just now"

    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min ago"

    hours = minutes // 60
    if hours < 24:
        return f"{hours} hr{'s' if hours > 1 else ''} ago"

    days = hours // 24
    if days < 30:
        return f"{days} day{'s' if days > 1 else ''} ago"

    months = days // 30
    return f"{months} mo ago"


@router.get(
    "/stats",
    response_model=StatsResponse,
    summary="Headline metrics for the stat cards",
)
async def read_stats(
    db: DBSession,
    current_user: CurrentUser,
    response: Response,
    window_days: Annotated[
        int,
        Query(ge=1, le=365, description="Length of the window each stat covers."),
    ] = DEFAULT_WINDOW_DAYS,
) -> StatsResponse:
    """Four headline metrics, each with its change against the previous window.

    Every stat is scoped to the same trailing window rather than mixing
    all-time totals with month-over-month deltas - otherwise the delta describes
    a different population than the number above it.

    * **Documents Processed** - uploads received in the window.
    * **Pages Extracted** - pages the OCR stage read.
    * **Data Processed** - total upload size. The accompanying `unit` scales
      (KB/MB/GB) so a small installation does not read as `0.0 MB`.
    * **Success Rate** - completed / (completed + failed), so documents still in
      flight neither help nor hurt the figure. Its delta is in percentage
      *points*.
    """
    response.headers["Cache-Control"] = _CACHE_CONTROL

    now = datetime.now(UTC)
    window = timedelta(days=window_days)
    owner_id = _owner_scope(current_user)

    current = await dashboard_repo.window_metrics(
        db, start=now - window, end=now, owner_id=owner_id
    )
    previous = await dashboard_repo.window_metrics(
        db, start=now - window * 2, end=now - window, owner_id=owner_id
    )

    comparison = f"vs. previous {window_days} days"
    documents_delta, documents_trend = _percent_change(
        current.documents, previous.documents
    )
    pages_delta, pages_trend = _percent_change(current.pages, previous.pages)
    size_delta, size_trend = _percent_change(current.size_bytes, previous.size_bytes)
    rate_delta, rate_trend = _point_change(current.success_rate, previous.success_rate)
    data_value, data_unit = _data_volume(current.size_bytes)

    return StatsResponse(
        stats=[
            StatCard(
                id="documents",
                label="Documents Processed",
                value=current.documents,
                format=StatFormat.NUMBER,
                delta=documents_delta,
                trend=documents_trend,
                comparison=comparison,
            ),
            StatCard(
                id="pages",
                label="Pages Extracted",
                value=current.pages,
                format=StatFormat.NUMBER,
                delta=pages_delta,
                trend=pages_trend,
                comparison=comparison,
            ),
            StatCard(
                id="data",
                label="Data Processed",
                value=data_value,
                format=StatFormat.NUMBER,
                delta=size_delta,
                trend=size_trend,
                unit=data_unit,
                comparison=comparison,
            ),
            StatCard(
                id="success_rate",
                label="Success Rate",
                value=round(current.success_rate, 1),
                format=StatFormat.PERCENT,
                delta=rate_delta,
                trend=rate_trend,
                comparison=comparison,
            ),
        ],
        window_days=window_days,
        generated_at=now,
    )


@router.get(
    "/revenue",
    response_model=SeriesResponse,
    summary="Monthly volume series for the chart",
)
async def read_series(
    db: DBSession,
    current_user: CurrentUser,
    response: Response,
    months: Annotated[
        int, Query(ge=1, le=12, description="Number of trailing calendar months.")
    ] = DEFAULT_SERIES_MONTHS,
) -> SeriesResponse:
    """Documents uploaded per calendar month, oldest first.

    The path is ``/revenue`` because that is what the deployed SPA bundle
    requests. This service processes documents and stores no financial data, so
    the honest series behind that panel is upload volume - and the response
    carries its own `meta.title`/`meta.subtitle` so the chart can caption itself
    accurately instead of trusting a hard-coded "Revenue" label.

    Months with no uploads are returned as explicit zeros, so the chart shows a
    real gap rather than silently compressing its x-axis.
    """
    response.headers["Cache-Control"] = _CACHE_CONTROL

    now = datetime.now(UTC)
    buckets = dashboard_repo.month_buckets(reference=now, months=months)
    counts = await dashboard_repo.monthly_document_counts(
        db, buckets=buckets, owner_id=_owner_scope(current_user)
    )

    points = [
        SeriesPoint(label=bucket.label, value=count)
        for bucket, count in zip(buckets, counts, strict=True)
    ]

    return SeriesResponse(
        series=points,
        meta=SeriesMeta(
            title="Document Volume",
            subtitle=f"Documents uploaded per month, last {len(points)} months",
            unit="documents",
            total=sum(point.value for point in points),
            year=buckets[-1].year,
        ),
    )


@router.get(
    "/activity",
    response_model=ActivityResponse,
    summary="Recent document activity feed",
)
async def read_activity(
    db: DBSession,
    current_user: CurrentUser,
    response: Response,
    limit: Annotated[
        int, Query(ge=1, le=50, description="Maximum number of events to return.")
    ] = DEFAULT_ACTIVITY_LIMIT,
) -> ActivityResponse:
    """The newest lifecycle events, newest first.

    This is the existing document audit trail rendered for humans: the same rows
    that answer "why did this fail at 2am" also make an honest activity feed, so
    nothing here is synthesised for display.

    An unrecognised event name still renders - it falls back to the raw name with
    a neutral icon key - so adding a new event type to the pipeline can never
    blank out this panel.
    """
    response.headers["Cache-Control"] = _CACHE_CONTROL

    now = datetime.now(UTC)
    rows = await dashboard_repo.recent_activity(
        db, limit=limit, owner_id=_owner_scope(current_user)
    )

    items: list[ActivityItem] = []
    for row in rows:
        phrase, kind = _EVENT_PRESENTATION.get(
            row.event, (row.event.replace("_", " "), "processing")
        )
        occurred_at = (
            row.occurred_at
            if row.occurred_at.tzinfo
            else row.occurred_at.replace(tzinfo=UTC)
        )
        items.append(
            ActivityItem(
                id=row.event_id,
                user=row.owner_name or row.owner_email,
                action=f"{phrase} {row.filename}",
                # The failure reason is more useful than a file size on the one
                # row where something went wrong.
                amount=(
                    (row.document_type or "failed")
                    if row.status is DocumentStatus.FAILED
                    else _human_size(row.size_bytes)
                ),
                time=_relative_time(occurred_at, now=now),
                type=kind,
                document_id=row.document_id,
                filename=row.filename,
                document_type=row.document_type,
                timestamp=occurred_at,
            )
        )

    return ActivityResponse(activity=items)
