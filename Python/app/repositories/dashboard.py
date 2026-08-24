"""Read-only aggregation queries behind the dashboard endpoints.

Two constraints shape everything here:

* **One round trip per panel.** The dashboard is the most-polled screen in the
  app, so each panel resolves to a single aggregate query rather than a query
  per metric or - worse - a query per row.
* **No dialect-specific date functions.** Bucketing by month with ``strftime``
  (SQLite) or ``DATE_FORMAT`` (MySQL) would tie the dashboard to one database.
  Month boundaries are therefore computed in Python and pushed down as plain
  range comparisons, which every backend can index.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Select, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.expression import ColumnElement

from app.models.document import Document, DocumentEvent, DocumentStatus
from app.models.user import User

# Hard-coded rather than ``strftime("%b")``: month abbreviations come from the C
# library's locale, so a host set to de_DE would silently start returning "Mai"
# to an English UI.
_MONTH_ABBREVIATIONS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


@dataclass(frozen=True, slots=True)
class WindowMetrics:
    """Document totals for one time window."""

    documents: int
    completed: int
    failed: int
    in_progress: int
    pages: int
    size_bytes: int

    @property
    def finished(self) -> int:
        """Documents that reached a terminal state - the success-rate divisor."""
        return self.completed + self.failed

    @property
    def success_rate(self) -> float:
        """Percentage of finished documents that completed.

        Zero when nothing finished, rather than a division error or a flattering
        100%: "no data yet" must not render as "everything worked".
        """
        if self.finished == 0:
            return 0.0
        return self.completed / self.finished * 100


@dataclass(frozen=True, slots=True)
class MonthBucket:
    """One calendar month, half-open ``[start, end)``."""

    label: str
    year: int
    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class ActivityRow:
    """One audit event, flattened with the columns the feed needs."""

    event_id: int
    event: str
    message: str | None
    occurred_at: datetime
    document_id: int
    filename: str
    size_bytes: int
    document_type: str | None
    status: DocumentStatus
    owner_name: str | None
    owner_email: str


def _scope(stmt: Select[tuple], owner_id: int | None) -> Select[tuple]:
    """Restrict a query to one owner. ``None`` means "every user".

    Only an admin reaches the ``None`` branch - see the endpoint module, which
    derives this from the caller's role rather than from a query parameter.
    """
    if owner_id is None:
        return stmt
    return stmt.where(Document.owner_id == owner_id)


def _count_if(condition: ColumnElement[bool]) -> ColumnElement[int]:
    """``COUNT`` of the rows matching ``condition``, as a ``SUM(CASE ...)``.

    ``COUNT(*) FILTER (WHERE ...)`` would read better but is Postgres/SQLite
    only, and this service also runs on MySQL.
    """
    return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)


def month_buckets(*, reference: datetime, months: int) -> list[MonthBucket]:
    """The ``months`` calendar months ending with the one containing ``reference``.

    Capped at 12: the labels are month abbreviations, so a 13-month span would
    emit "Jan" twice and break any client keying a chart by label.
    """
    months = max(1, min(months, 12))

    # Walk back to the first bucket, then forward, so the result comes out in
    # chronological order with no reversal.
    offset = reference.month - months
    year = reference.year + offset // 12
    month = offset % 12 + 1

    buckets: list[MonthBucket] = []
    for _ in range(months):
        next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
        buckets.append(
            MonthBucket(
                label=_MONTH_ABBREVIATIONS[month - 1],
                year=year,
                start=datetime(year, month, 1, tzinfo=UTC),
                end=datetime(next_year, next_month, 1, tzinfo=UTC),
            )
        )
        year, month = next_year, next_month

    return buckets


async def window_metrics(
    db: AsyncSession,
    *,
    start: datetime,
    end: datetime,
    owner_id: int | None = None,
) -> WindowMetrics:
    """Aggregate every stat-card metric for ``[start, end)`` in one query."""
    stmt = select(
        func.count(Document.id),
        _count_if(Document.status == DocumentStatus.COMPLETED),
        _count_if(Document.status == DocumentStatus.FAILED),
        _count_if(
            Document.status.in_((DocumentStatus.PENDING, DocumentStatus.PROCESSING))
        ),
        func.coalesce(func.sum(Document.page_count), 0),
        func.coalesce(func.sum(Document.size_bytes), 0),
    ).where(Document.created_at >= start, Document.created_at < end)

    row = (await db.execute(_scope(stmt, owner_id))).one()
    return WindowMetrics(
        documents=int(row[0] or 0),
        completed=int(row[1] or 0),
        failed=int(row[2] or 0),
        in_progress=int(row[3] or 0),
        pages=int(row[4] or 0),
        size_bytes=int(row[5] or 0),
    )


async def monthly_document_counts(
    db: AsyncSession,
    *,
    buckets: list[MonthBucket],
    owner_id: int | None = None,
) -> list[int]:
    """Upload volume per month, in the order of ``buckets``.

    One query with a conditional sum per bucket, not one query per month, so the
    twelve-month view is still a single round trip.
    """
    if not buckets:
        return []

    stmt = select(
        *(
            _count_if(
                (Document.created_at >= bucket.start)
                & (Document.created_at < bucket.end)
            ).label(f"month_{index}")
            for index, bucket in enumerate(buckets)
        )
    ).where(
        # Redundant with the CASE expressions, but it lets the index on
        # created_at skip every older row instead of scanning the table.
        Document.created_at >= buckets[0].start,
        Document.created_at < buckets[-1].end,
    )

    row = (await db.execute(_scope(stmt, owner_id))).one()
    return [int(value or 0) for value in row]


async def recent_activity(
    db: AsyncSession, *, limit: int = 8, owner_id: int | None = None
) -> list[ActivityRow]:
    """The newest audit events, joined to their document and owner.

    Explicit columns rather than whole entities: the relationships are declared
    ``lazy="raise"``, and the feed needs six fields - not the multi-megabyte
    extraction row hanging off each document.
    """
    stmt = (
        select(
            DocumentEvent.id,
            DocumentEvent.event,
            DocumentEvent.message,
            DocumentEvent.created_at,
            Document.id,
            Document.filename,
            Document.size_bytes,
            Document.document_type,
            Document.status,
            User.full_name,
            User.email,
        )
        .join(Document, Document.id == DocumentEvent.document_id)
        .join(User, User.id == Document.owner_id)
        # Tie-break on the event id: several events can share a timestamp, and
        # without this their relative order flips between calls.
        .order_by(DocumentEvent.created_at.desc(), DocumentEvent.id.desc())
        .limit(limit)
    )

    rows = (await db.execute(_scope(stmt, owner_id))).all()
    return [
        ActivityRow(
            event_id=int(row[0]),
            event=str(row[1]),
            message=row[2],
            occurred_at=row[3],
            document_id=int(row[4]),
            filename=str(row[5]),
            size_bytes=int(row[6] or 0),
            document_type=row[7],
            status=row[8],
            owner_name=row[9],
            owner_email=str(row[10]),
        )
        for row in rows
    ]
