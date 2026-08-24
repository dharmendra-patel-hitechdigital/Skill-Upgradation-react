"""Aggregation queries behind the document analytics endpoint.

Every figure here comes from a column the pipeline already persists -
``ocr_duration_ms``, ``analysis_duration_ms``, ``confidence``, ``prompt_tokens``,
``error_code`` - so this is a read-only view over existing data. Nothing was
added to the schema for it.

Two deliberate choices:

* **Pipeline time, not wall-clock time.** ``ocr_duration_ms +
  analysis_duration_ms`` is the work actually done. End-to-end wall clock
  includes however long the document sat in the queue behind other uploads,
  which says more about concurrency than about the document, and cannot be
  ordered in portable SQL anyway (it is derived from two datetimes).
* **Percentiles by OFFSET, not by loading every row.** ``PERCENTILE_CONT`` is
  not available on SQLite or MySQL 8, and pulling every duration into Python to
  sort it does not scale. Counting, then seeking to the nth row, does.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import Select, case, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.expression import ColumnElement

from app.models.document import Document, DocumentExtraction, DocumentStatus
from app.models.user import User

# Buckets for the confidence histogram. Deliberately coarse and deliberately
# split at 0.5/0.7/0.85: the question an operator asks is "how much of this
# needs a human to look at it", not "what is the exact distribution".
CONFIDENCE_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("Very low (<50%)", 0.0, 0.5),
    ("Low (50-70%)", 0.5, 0.7),
    ("Fair (70-85%)", 0.7, 0.85),
    ("High (85%+)", 0.85, 1.01),
)


@dataclass(frozen=True, slots=True)
class Totals:
    documents: int
    completed: int
    failed: int
    in_progress: int
    pages: int
    size_bytes: int
    reprocessed: int


@dataclass(frozen=True, slots=True)
class TypeBreakdown:
    document_type: str | None
    documents: int
    failed: int
    avg_confidence: float | None
    avg_pages: float | None


@dataclass(frozen=True, slots=True)
class FailureBreakdown:
    code: str
    documents: int
    example_message: str | None
    latest_at: datetime | None


@dataclass(frozen=True, slots=True)
class Performance:
    samples: int
    avg_ocr_ms: float | None
    avg_analysis_ms: float | None
    avg_total_ms: float | None
    p50_total_ms: int | None
    p95_total_ms: int | None
    slowest_total_ms: int | None
    avg_ms_per_page: float | None


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    stage: str
    provider: str
    documents: int


@dataclass(frozen=True, slots=True)
class TokenUsage:
    prompt_tokens: int
    completion_tokens: int
    documents_with_tokens: int


@dataclass(frozen=True, slots=True)
class Bucket:
    label: str
    documents: int


@dataclass(frozen=True, slots=True)
class UploaderUsage:
    owner_id: int
    email: str
    full_name: str | None
    documents: int
    failed: int


@dataclass(frozen=True, slots=True)
class DayPoint:
    date: str
    documents: int
    completed: int
    failed: int


@dataclass(frozen=True, slots=True)
class DocumentAnalytics:
    totals: Totals
    by_type: list[TypeBreakdown] = field(default_factory=list)
    failures: list[FailureBreakdown] = field(default_factory=list)
    performance: Performance | None = None
    providers: list[ProviderUsage] = field(default_factory=list)
    tokens: TokenUsage | None = None
    confidence: list[Bucket] = field(default_factory=list)
    uploaders: list[UploaderUsage] = field(default_factory=list)
    daily: list[DayPoint] = field(default_factory=list)


# ------------------------------------------------------------------- helpers
def _count_if(condition: ColumnElement[bool]) -> ColumnElement[int]:
    """``COUNT`` of rows matching ``condition``. Portable ``FILTER (WHERE ...)``."""
    return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)


def _window(stmt: Select[tuple], start: datetime, end: datetime, owner_id: int | None):
    """Restrict to the reporting window, and to one owner unless admin."""
    stmt = stmt.where(Document.created_at >= start, Document.created_at < end)
    if owner_id is not None:
        stmt = stmt.where(Document.owner_id == owner_id)
    return stmt


# Total time the pipeline actually spent on a document.
_PIPELINE_MS = func.coalesce(DocumentExtraction.ocr_duration_ms, 0) + func.coalesce(
    DocumentExtraction.analysis_duration_ms, 0
)


async def collect(
    db: AsyncSession,
    *,
    start: datetime,
    end: datetime,
    owner_id: int | None = None,
    top_uploaders: int = 5,
    include_uploaders: bool = True,
) -> DocumentAnalytics:
    """Gather every panel for one window.

    One aggregate query per panel rather than one combined monster: they group by
    different columns and join different tables, so combining them would need
    subqueries that are harder to read and no faster.
    """
    totals = await _totals(db, start, end, owner_id)
    return DocumentAnalytics(
        totals=totals,
        by_type=await _by_type(db, start, end, owner_id),
        failures=await _failures(db, start, end, owner_id),
        performance=await _performance(db, start, end, owner_id),
        providers=await _providers(db, start, end, owner_id),
        tokens=await _tokens(db, start, end, owner_id),
        confidence=await _confidence(db, start, end, owner_id),
        uploaders=(
            await _uploaders(db, start, end, owner_id, top_uploaders)
            if include_uploaders
            else []
        ),
        daily=await _daily(db, start, end, owner_id),
    )


async def _totals(
    db: AsyncSession, start: datetime, end: datetime, owner_id: int | None
) -> Totals:
    stmt = select(
        func.count(Document.id),
        _count_if(Document.status == DocumentStatus.COMPLETED),
        _count_if(Document.status == DocumentStatus.FAILED),
        _count_if(
            Document.status.in_((DocumentStatus.PENDING, DocumentStatus.PROCESSING))
        ),
        func.coalesce(func.sum(Document.page_count), 0),
        func.coalesce(func.sum(Document.size_bytes), 0),
        # attempt_count > 1 means the pipeline ran more than once - a retry after
        # a failure, or a deliberate reprocess. Either way it is repeated work.
        _count_if(Document.attempt_count > 1),
    )
    row = (await db.execute(_window(stmt, start, end, owner_id))).one()
    return Totals(
        documents=int(row[0] or 0),
        completed=int(row[1] or 0),
        failed=int(row[2] or 0),
        in_progress=int(row[3] or 0),
        pages=int(row[4] or 0),
        size_bytes=int(row[5] or 0),
        reprocessed=int(row[6] or 0),
    )


async def _by_type(
    db: AsyncSession, start: datetime, end: datetime, owner_id: int | None
) -> list[TypeBreakdown]:
    """Per classification: volume, failures, and how confident the analyser was.

    LEFT JOIN, not INNER: a document that failed before analysis has no
    extraction row, and dropping it would understate exactly the class of
    document an operator is looking for.
    """
    stmt = (
        select(
            Document.document_type,
            func.count(Document.id),
            _count_if(Document.status == DocumentStatus.FAILED),
            func.avg(DocumentExtraction.confidence),
            func.avg(Document.page_count),
        )
        .outerjoin(DocumentExtraction, DocumentExtraction.document_id == Document.id)
        .group_by(Document.document_type)
        .order_by(desc(func.count(Document.id)))
    )
    rows = (await db.execute(_window(stmt, start, end, owner_id))).all()
    return [
        TypeBreakdown(
            document_type=row[0],
            documents=int(row[1] or 0),
            failed=int(row[2] or 0),
            avg_confidence=float(row[3]) if row[3] is not None else None,
            avg_pages=float(row[4]) if row[4] is not None else None,
        )
        for row in rows
    ]


async def _failures(
    db: AsyncSession, start: datetime, end: datetime, owner_id: int | None
) -> list[FailureBreakdown]:
    """Failures grouped by error code - the highest-value panel here.

    ``example_message`` is ``MAX()`` over the group, so it is *a* real message
    from that group rather than the newest one. Named accordingly: pretending it
    corresponds to ``latest_at`` would be a lie a reader could act on.
    """
    stmt = (
        select(
            Document.error_code,
            func.count(Document.id),
            func.max(Document.error_message),
            func.max(Document.updated_at),
        )
        .where(
            Document.status == DocumentStatus.FAILED,
            Document.error_code.is_not(None),
        )
        .group_by(Document.error_code)
        .order_by(desc(func.count(Document.id)))
    )
    rows = (await db.execute(_window(stmt, start, end, owner_id))).all()
    return [
        FailureBreakdown(
            code=str(row[0]),
            documents=int(row[1] or 0),
            example_message=row[2],
            latest_at=row[3],
        )
        for row in rows
    ]


async def _performance(
    db: AsyncSession, start: datetime, end: datetime, owner_id: int | None
) -> Performance:
    base = select(DocumentExtraction).join(
        Document, Document.id == DocumentExtraction.document_id
    )

    averages = select(
        func.count(DocumentExtraction.id),
        func.avg(DocumentExtraction.ocr_duration_ms),
        func.avg(DocumentExtraction.analysis_duration_ms),
        func.avg(_PIPELINE_MS),
        func.max(_PIPELINE_MS),
        # Per page rather than per document: a 40-page scan being slower than a
        # one-page receipt is not a finding.
        func.avg(
            _PIPELINE_MS
            / func.nullif(func.coalesce(DocumentExtraction.page_count, 1), 0)
        ),
    ).join(Document, Document.id == DocumentExtraction.document_id)

    row = (await db.execute(_window(averages, start, end, owner_id))).one()
    samples = int(row[0] or 0)
    if samples == 0:
        return Performance(0, None, None, None, None, None, None, None)

    p50 = await _percentile(db, base, start, end, owner_id, samples, 0.50)
    p95 = await _percentile(db, base, start, end, owner_id, samples, 0.95)

    return Performance(
        samples=samples,
        avg_ocr_ms=float(row[1]) if row[1] is not None else None,
        avg_analysis_ms=float(row[2]) if row[2] is not None else None,
        avg_total_ms=float(row[3]) if row[3] is not None else None,
        p50_total_ms=p50,
        p95_total_ms=p95,
        slowest_total_ms=int(row[4]) if row[4] is not None else None,
        avg_ms_per_page=float(row[5]) if row[5] is not None else None,
    )


async def _percentile(
    db: AsyncSession,
    base: Select[tuple],
    start: datetime,
    end: datetime,
    owner_id: int | None,
    samples: int,
    fraction: float,
) -> int | None:
    """Nearest-rank percentile: sort, then seek to the nth row.

    Clamped to the last index so p95 of a two-row sample is the slower of the
    two rather than an OFFSET past the end (which returns nothing).
    """
    offset = min(samples - 1, int(samples * fraction))
    stmt = (
        select(_PIPELINE_MS)
        .join(Document, Document.id == DocumentExtraction.document_id)
        .order_by(_PIPELINE_MS)
        .offset(offset)
        .limit(1)
    )
    value = await db.scalar(_window(stmt, start, end, owner_id))
    return int(value) if value is not None else None


async def _providers(
    db: AsyncSession, start: datetime, end: datetime, owner_id: int | None
) -> list[ProviderUsage]:
    """Which engine actually ran, per stage.

    Answers "is this installation getting Textract/OpenAI results or the
    built-in fallbacks?" - which is the first thing to check when quality is
    disappointing, because the fallbacks engage silently by design.
    """
    usage: list[ProviderUsage] = []
    for stage, column in (
        ("text_extraction", DocumentExtraction.ocr_provider),
        ("analysis", DocumentExtraction.analysis_provider),
    ):
        stmt = (
            select(column, func.count(DocumentExtraction.id))
            .join(Document, Document.id == DocumentExtraction.document_id)
            .group_by(column)
            .order_by(desc(func.count(DocumentExtraction.id)))
        )
        rows = (await db.execute(_window(stmt, start, end, owner_id))).all()
        usage.extend(
            ProviderUsage(stage=stage, provider=str(row[0]), documents=int(row[1] or 0))
            for row in rows
        )
    return usage


async def _tokens(
    db: AsyncSession, start: datetime, end: datetime, owner_id: int | None
) -> TokenUsage:
    """Token totals - the closest thing to a spend figure this service holds.

    Null for the heuristic analyser, which costs nothing, so
    ``documents_with_tokens`` is what says whether the totals mean anything.
    """
    stmt = select(
        func.coalesce(func.sum(DocumentExtraction.prompt_tokens), 0),
        func.coalesce(func.sum(DocumentExtraction.completion_tokens), 0),
        _count_if(DocumentExtraction.prompt_tokens.is_not(None)),
    ).join(Document, Document.id == DocumentExtraction.document_id)

    row = (await db.execute(_window(stmt, start, end, owner_id))).one()
    return TokenUsage(
        prompt_tokens=int(row[0] or 0),
        completion_tokens=int(row[1] or 0),
        documents_with_tokens=int(row[2] or 0),
    )


async def _confidence(
    db: AsyncSession, start: datetime, end: datetime, owner_id: int | None
) -> list[Bucket]:
    """Histogram of analyser confidence, in one pass over the joined rows."""
    stmt = select(
        *(
            _count_if(
                (DocumentExtraction.confidence >= low)
                & (DocumentExtraction.confidence < high)
            ).label(f"bucket_{index}")
            for index, (_, low, high) in enumerate(CONFIDENCE_BUCKETS)
        )
    ).join(Document, Document.id == DocumentExtraction.document_id)

    row = (await db.execute(_window(stmt, start, end, owner_id))).one()
    return [
        Bucket(label=label, documents=int(value or 0))
        for (label, _, _), value in zip(CONFIDENCE_BUCKETS, row, strict=True)
    ]


async def _uploaders(
    db: AsyncSession,
    start: datetime,
    end: datetime,
    owner_id: int | None,
    limit: int,
) -> list[UploaderUsage]:
    stmt = (
        select(
            User.id,
            User.email,
            User.full_name,
            func.count(Document.id),
            _count_if(Document.status == DocumentStatus.FAILED),
        )
        .join(User, User.id == Document.owner_id)
        .group_by(User.id, User.email, User.full_name)
        .order_by(desc(func.count(Document.id)))
        .limit(limit)
    )
    rows = (await db.execute(_window(stmt, start, end, owner_id))).all()
    return [
        UploaderUsage(
            owner_id=int(row[0]),
            email=str(row[1]),
            full_name=row[2],
            documents=int(row[3] or 0),
            failed=int(row[4] or 0),
        )
        for row in rows
    ]


async def _daily(
    db: AsyncSession, start: datetime, end: datetime, owner_id: int | None
) -> list[DayPoint]:
    """Per-day throughput, bucketed in Python.

    Deliberately not ``GROUP BY DATE(created_at)``: that is ``strftime`` on
    SQLite and ``DATE()`` on MySQL, and the resulting string differs in edge
    cases. Selecting two narrow columns and bucketing here keeps one code path
    across every backend, and reads one row per document in the window rather
    than the whole table.
    """
    stmt = select(Document.created_at, Document.status)
    rows = (await db.execute(_window(stmt, start, end, owner_id))).all()

    counters: dict[str, list[int]] = {}
    for created_at, status in rows:
        key = created_at.date().isoformat()
        entry = counters.setdefault(key, [0, 0, 0])
        entry[0] += 1
        if status == DocumentStatus.COMPLETED:
            entry[1] += 1
        elif status == DocumentStatus.FAILED:
            entry[2] += 1

    return [
        DayPoint(date=day, documents=values[0], completed=values[1], failed=values[2])
        for day, values in sorted(counters.items())
    ]
