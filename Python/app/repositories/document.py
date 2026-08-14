"""Document, extraction, and event data access."""
from __future__ import annotations

from typing import Any

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.base import utcnow
from app.models.document import (
    Document,
    DocumentEvent,
    DocumentExtraction,
    DocumentStatus,
)
from app.schemas.document import (
    DocumentFilters,
    DocumentSortField,
    SortDirection,
)

_SORT_COLUMNS = {
    DocumentSortField.CREATED_AT: Document.created_at,
    DocumentSortField.UPDATED_AT: Document.updated_at,
    DocumentSortField.FILENAME: Document.filename,
    DocumentSortField.SIZE_BYTES: Document.size_bytes,
}


# ------------------------------------------------------------------- documents
async def create(
    db: AsyncSession,
    *,
    owner_id: int,
    filename: str,
    content_type: str,
    size_bytes: int,
    checksum_sha256: str,
    storage_key: str,
    storage_backend: str,
) -> Document:
    document = Document(
        owner_id=owner_id,
        filename=filename,
        content_type=content_type,
        size_bytes=size_bytes,
        checksum_sha256=checksum_sha256,
        storage_key=storage_key,
        storage_backend=storage_backend,
        status=DocumentStatus.PENDING,
    )
    db.add(document)
    await db.flush()
    return document


async def get(
    db: AsyncSession,
    document_id: int,
    *,
    owner_id: int | None = None,
    with_details: bool = False,
) -> Document | None:
    """Fetch one document.

    ``owner_id`` scopes the lookup so an authorisation failure is impossible to
    forget: a caller who does not own the row simply gets ``None`` (-> 404),
    which also avoids leaking that the id exists at all.

    ``with_details`` eagerly loads the extraction and event trail. Relationships
    are declared ``lazy="raise"``, so anything that touches them without this
    flag fails loudly in tests instead of silently N+1-ing in production.
    """
    stmt = select(Document).where(Document.id == document_id)
    if owner_id is not None:
        stmt = stmt.where(Document.owner_id == owner_id)
    if with_details:
        stmt = stmt.options(
            selectinload(Document.extraction), selectinload(Document.events)
        )
    return await db.scalar(stmt)


async def get_by_checksum(
    db: AsyncSession, *, owner_id: int, checksum: str
) -> Document | None:
    """Find this owner's existing copy of an identical file (deduplication)."""
    stmt = select(Document).where(
        Document.owner_id == owner_id, Document.checksum_sha256 == checksum
    )
    return await db.scalar(stmt)


def _apply_filters(stmt: Select[Any], filters: DocumentFilters) -> Select[Any]:
    if filters.status is not None:
        stmt = stmt.where(Document.status == filters.status)
    if filters.document_type:
        stmt = stmt.where(Document.document_type == filters.document_type)
    if filters.search:
        # ILIKE-equivalent: SQLAlchemy's `ilike` maps to LOWER(..) LIKE LOWER(..)
        # on backends without a native case-insensitive LIKE.
        needle = f"%{filters.search.strip()}%"
        stmt = stmt.where(Document.filename.ilike(needle))
    return stmt


async def list_documents(
    db: AsyncSession,
    *,
    filters: DocumentFilters,
    offset: int,
    limit: int,
    owner_id: int | None = None,
) -> tuple[list[Document], int]:
    """Return one page of documents plus the total number of matches.

    ``owner_id=None`` means "across all users" and is only reachable from an
    admin-guarded route.
    """
    base = select(Document)
    if owner_id is not None:
        base = base.where(Document.owner_id == owner_id)
    base = _apply_filters(base, filters)

    total = int(
        await db.scalar(select(func.count()).select_from(base.subquery())) or 0
    )

    column = _SORT_COLUMNS[filters.sort_by]
    order = column.desc() if filters.sort_dir is SortDirection.DESC else column.asc()
    # Tie-break on the primary key so paging is stable when the sort column has
    # duplicate values (e.g. two uploads in the same second).
    stmt = base.order_by(order, Document.id.desc()).offset(offset).limit(limit)
    rows = list((await db.scalars(stmt)).all())
    return rows, total


async def delete(db: AsyncSession, document: Document) -> None:
    """Remove the row; extraction and events cascade."""
    await db.delete(document)
    await db.flush()


# ------------------------------------------------------- lifecycle transitions
async def claim_for_processing(db: AsyncSession, document_id: int) -> bool:
    """Atomically move PENDING -> PROCESSING. Returns False if already claimed.

    The status check lives in the UPDATE's WHERE clause, so the database - not
    application code - decides the winner. Two workers racing on the same
    document therefore cannot both start the (billable) AI pipeline.
    """
    stmt = (
        update(Document)
        .where(Document.id == document_id, Document.status == DocumentStatus.PENDING)
        .values(
            status=DocumentStatus.PROCESSING,
            processing_started_at=utcnow(),
            processing_finished_at=None,
            attempt_count=Document.attempt_count + 1,
            error_code=None,
            error_message=None,
            updated_at=utcnow(),
        )
    )
    result = await db.execute(stmt)
    return bool(result.rowcount)


async def mark_completed(
    db: AsyncSession,
    document_id: int,
    *,
    document_type: str | None,
    page_count: int | None,
) -> None:
    stmt = (
        update(Document)
        .where(Document.id == document_id)
        .values(
            status=DocumentStatus.COMPLETED,
            processing_finished_at=utcnow(),
            document_type=document_type,
            page_count=page_count,
            error_code=None,
            error_message=None,
            updated_at=utcnow(),
        )
    )
    await db.execute(stmt)


async def mark_failed(
    db: AsyncSession, document_id: int, *, code: str, message: str
) -> None:
    stmt = (
        update(Document)
        .where(Document.id == document_id)
        .values(
            status=DocumentStatus.FAILED,
            processing_finished_at=utcnow(),
            error_code=code[:64],
            error_message=message[:1024],
            updated_at=utcnow(),
        )
    )
    await db.execute(stmt)


async def reset_to_pending(db: AsyncSession, document: Document) -> None:
    """Queue an already-processed document for another run."""
    document.status = DocumentStatus.PENDING
    document.error_code = None
    document.error_message = None
    document.processing_started_at = None
    document.processing_finished_at = None
    await db.flush()


async def count_by_status(db: AsyncSession, *, owner_id: int | None = None) -> dict[str, int]:
    """Aggregate counts per status - one GROUP BY, not one query per status."""
    stmt = select(Document.status, func.count()).group_by(Document.status)
    if owner_id is not None:
        stmt = stmt.where(Document.owner_id == owner_id)
    rows = (await db.execute(stmt)).all()
    counts = {status.value: 0 for status in DocumentStatus}
    for status, quantity in rows:
        key = status.value if isinstance(status, DocumentStatus) else str(status)
        counts[key] = int(quantity)
    return counts


# ------------------------------------------------------------------ extraction
async def upsert_extraction(
    db: AsyncSession, document_id: int, values: dict[str, Any]
) -> DocumentExtraction:
    """Create or replace the extraction row for a document.

    Reprocessing overwrites in place rather than appending, so a document always
    has exactly one current result and the detail response stays unambiguous.
    """
    existing = await db.scalar(
        select(DocumentExtraction).where(DocumentExtraction.document_id == document_id)
    )
    if existing is None:
        existing = DocumentExtraction(document_id=document_id, **values)
        db.add(existing)
    else:
        for field, value in values.items():
            setattr(existing, field, value)
    await db.flush()
    return existing


async def get_extraction(db: AsyncSession, document_id: int) -> DocumentExtraction | None:
    return await db.scalar(
        select(DocumentExtraction).where(DocumentExtraction.document_id == document_id)
    )


# ---------------------------------------------------------------------- events
async def add_event(
    db: AsyncSession,
    document_id: int,
    *,
    event: str,
    message: str | None = None,
    payload: dict[str, Any] | None = None,
) -> DocumentEvent:
    record = DocumentEvent(
        document_id=document_id,
        event=event[:48],
        message=message[:512] if message else None,
        payload=payload or {},
    )
    db.add(record)
    await db.flush()
    return record


async def list_stuck_processing(db: AsyncSession, *, older_than_seconds: float) -> list[Document]:
    """Documents left in PROCESSING by a crashed worker.

    In-process background tasks die with the process, so on startup we sweep
    these back to FAILED rather than leaving them stuck forever.
    """
    from datetime import timedelta

    cutoff = utcnow() - timedelta(seconds=older_than_seconds)
    stmt = select(Document).where(
        Document.status == DocumentStatus.PROCESSING,
        Document.processing_started_at.is_not(None),
        Document.processing_started_at < cutoff,
    )
    return list((await db.scalars(stmt)).all())
