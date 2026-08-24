"""Document, extraction-result, and audit-event models.

Shape of the data
-----------------
``Document``            - the file plus its processing lifecycle. Small, hot,
                          heavily queried (list/filter/paginate).
``DocumentExtraction``  - the AI output: raw text plus structured fields. Large
                          and read rarely (only on the detail view), so it lives
                          in its own table. Listing 50 documents therefore never
                          drags 50 megabytes of OCR text through the database.
``DocumentEvent``       - an append-only audit trail of state transitions. This
                          is what answers "why did this document fail at 2am,
                          and which provider was to blame".
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin, UtcDateTime, portable_enum, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.models.user import User

# MySQL's TEXT caps at 64 KB, which a 40-page scan will exceed. LONGTEXT on
# MySQL, plain TEXT everywhere else.
LongText = Text().with_variant(mysql.LONGTEXT(), "mysql")


class DocumentStatus(StrEnum):
    """Lifecycle of a document.

    Legal transitions (enforced in the service layer, see
    :func:`app.services.document_processor.assert_can_transition`)::

        PENDING    -> PROCESSING -> COMPLETED
                                 -> FAILED
        FAILED     -> PENDING      (retry / reprocess)
        COMPLETED  -> PENDING      (reprocess with a different provider)
    """

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (DocumentStatus.COMPLETED, DocumentStatus.FAILED)


_STATUS_ENUM = portable_enum(DocumentStatus)


class Document(TimestampMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (
        # The same file uploaded twice by the same user is deduplicated rather
        # than re-billed to the AI provider.
        UniqueConstraint("owner_id", "checksum_sha256", name="uq_documents_owner_checksum"),
        # Backs the default listing query: owner's documents, newest first,
        # optionally filtered by status.
        Index("ix_documents_owner_status_created", "owner_id", "status", "created_at"),
        # Backs the *administrator's* listing, which spans every owner and so
        # cannot use the leading owner_id column of the composite index above.
        # Without this, the admin document list degrades into a full scan plus a
        # sort as the table grows.
        Index("ix_documents_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    # ---- source file -------------------------------------------------------
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(127), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), index=True, nullable=False)

    # Opaque key resolved by the storage backend (a path for local, an object
    # key for S3). The application never builds file paths itself.
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    storage_backend: Mapped[str] = mapped_column(String(20), nullable=False)

    # ---- lifecycle ---------------------------------------------------------
    status: Mapped[DocumentStatus] = mapped_column(
        _STATUS_ENUM,
        default=DocumentStatus.PENDING,
        server_default=DocumentStatus.PENDING.value,
        nullable=False,
        index=True,
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    processing_started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    processing_finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    # ---- denormalised AI output -------------------------------------------
    # Copied out of the extraction row purely so the list endpoint can filter
    # and display without joining the heavy table.
    document_type: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ---- relationships -----------------------------------------------------
    owner: Mapped[User] = relationship(back_populates="documents", lazy="raise")
    extraction: Mapped[DocumentExtraction | None] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
        lazy="raise",
    )
    events: Mapped[list[DocumentEvent]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="DocumentEvent.id",
        lazy="raise",
    )

    @property
    def processing_duration_ms(self) -> int | None:
        if self.processing_started_at is None or self.processing_finished_at is None:
            return None
        delta = self.processing_finished_at - self.processing_started_at
        return int(delta.total_seconds() * 1000)


class DocumentExtraction(TimestampMixin, Base):
    """The result of one full pipeline run: OCR text + structured AI analysis."""

    __tablename__ = "document_extractions"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )

    # ---- stage 1: text layer ----------------------------------------------
    raw_text: Mapped[str] = mapped_column(LongText, nullable=False, default="")
    text_char_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ocr_provider: Mapped[str] = mapped_column(String(32), nullable=False)
    ocr_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ---- stage 2: analysis -------------------------------------------------
    analysis_provider: Mapped[str] = mapped_column(String(32), nullable=False)
    analysis_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    analysis_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    document_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Semi-structured output. JSON (not extra tables) because the shape varies
    # per document type - an invoice has line items, a contract has parties -
    # and we never query *inside* these values.
    keywords: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    entities: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    fields: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    document: Mapped[Document] = relationship(back_populates="extraction", lazy="raise")


class DocumentEvent(Base):
    """Append-only audit record of one lifecycle transition or notable action."""

    __tablename__ = "document_events"
    __table_args__ = (Index("ix_document_events_document_created", "document_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True, nullable=False
    )

    event: Mapped[str] = mapped_column(String(48), nullable=False)
    message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    # Events are immutable, so they carry only created_at (no TimestampMixin).
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, server_default=func.now(), nullable=False
    )

    document: Mapped[Document] = relationship(back_populates="events", lazy="raise")
