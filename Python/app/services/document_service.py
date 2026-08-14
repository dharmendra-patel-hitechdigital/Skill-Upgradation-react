"""Use-case layer for documents: upload, delete, and question answering.

Endpoints in :mod:`app.api.v1.endpoints.documents` stay thin - they handle HTTP
concerns and delegate here. Keeping the rules in this module means the same
upload logic is reachable from a CLI importer or a batch job without going
through Starlette.
"""
from __future__ import annotations

import hashlib
import logging

from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import (
    DocumentNotReadyError,
    PayloadTooLargeError,
    UnsupportedMediaTypeError,
    ValidationError,
)
from app.models.document import Document, DocumentStatus
from app.models.user import User
from app.repositories import document as doc_repo
from app.schemas.document import AskRequest, AskResponse
from app.services.ai.registry import get_analyzer
from app.services.storage import build_object_key, get_storage

logger = logging.getLogger(__name__)

# 1 MiB chunks: large enough to keep syscall overhead negligible, small enough
# that an oversized upload is rejected long before it is fully buffered.
_CHUNK_SIZE = 1024 * 1024

# Leading bytes that identify a format regardless of what the client declared.
# A browser will happily label a PDF "application/octet-stream", and an attacker
# will happily label an executable "application/pdf" - so we look ourselves.
_MAGIC_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
)

# Formats where sniffing is impossible (plain text has no signature).
_UNSNIFFABLE = frozenset({"text/plain", "text/csv", "text/markdown", "application/json"})


async def upload_document(
    db: AsyncSession, *, owner: User, upload: UploadFile
) -> tuple[Document, bool]:
    """Validate, store, and register an uploaded file.

    Returns ``(document, created)``. ``created=False`` means an identical file
    already exists for this user and the existing record was returned instead -
    re-uploading the same invoice should not pay for the same AI call twice.
    """
    data = await _read_within_limit(upload)
    if not data:
        raise ValidationError("The uploaded file is empty.")

    content_type = _resolve_content_type(upload, data)
    checksum = hashlib.sha256(data).hexdigest()

    existing = await doc_repo.get_by_checksum(db, owner_id=owner.id, checksum=checksum)
    if existing is not None:
        logger.info(
            "duplicate_upload_ignored",
            extra={"document_id": existing.id, "checksum": checksum[:12]},
        )
        return existing, False

    storage = get_storage()
    key = build_object_key(owner_id=owner.id, filename=upload.filename or "upload")
    # Store the blob before the row: an orphaned object is cheap to reap, whereas
    # a row pointing at a file that was never written is a permanent 500 on read.
    await storage.save(key, data)

    document = await doc_repo.create(
        db,
        owner_id=owner.id,
        filename=_display_filename(upload.filename),
        content_type=content_type,
        size_bytes=len(data),
        checksum_sha256=checksum,
        storage_key=key,
        storage_backend=storage.name,
    )
    await doc_repo.add_event(
        db,
        document.id,
        event="uploaded",
        message=f"Received {len(data)} bytes as {content_type}.",
        payload={"content_type": content_type, "size_bytes": len(data)},
    )
    return document, True


async def _read_within_limit(upload: UploadFile) -> bytes:
    """Buffer the upload, aborting as soon as it exceeds the configured limit.

    ``Content-Length`` is a client-supplied hint and can lie, so the real check
    is on bytes actually received.
    """
    limit = settings.max_upload_bytes
    chunks: list[bytes] = []
    total = 0

    while chunk := await upload.read(_CHUNK_SIZE):
        total += len(chunk)
        if total > limit:
            raise PayloadTooLargeError(
                f"The file exceeds the {settings.MAX_UPLOAD_SIZE_MB} MB upload limit.",
                details={"limit_bytes": limit},
            )
        chunks.append(chunk)

    return b"".join(chunks)


def _resolve_content_type(upload: UploadFile, data: bytes) -> str:
    """Decide the real content type, then check it against the allow-list.

    The sniffed type wins over the declared one. Anything unrecognised falls back
    to the declared type only if that type is one we cannot sniff (plain text).
    """
    declared = (upload.content_type or "").split(";")[0].strip().lower()

    sniffed: str | None = None
    for signature, mime in _MAGIC_SIGNATURES:
        if data.startswith(signature):
            sniffed = mime
            break

    if sniffed is None and declared in _UNSNIFFABLE:
        if b"\x00" in data[:8192]:
            # Null bytes mean this is binary, whatever the client claimed.
            raise UnsupportedMediaTypeError(
                "The file was declared as text but contains binary data."
            )
        sniffed = declared

    if sniffed is None:
        raise UnsupportedMediaTypeError(
            "The file's format could not be identified. Supported formats: "
            + ", ".join(sorted(settings.ALLOWED_UPLOAD_TYPES)),
            details={"declared_content_type": declared or None},
        )

    if sniffed not in settings.ALLOWED_UPLOAD_TYPES:
        raise UnsupportedMediaTypeError(
            f"'{sniffed}' files are not accepted. Supported formats: "
            + ", ".join(sorted(settings.ALLOWED_UPLOAD_TYPES)),
            details={"detected_content_type": sniffed},
        )

    if declared and declared != sniffed:
        logger.info(
            "content_type_mismatch",
            extra={"declared": declared, "detected": sniffed},
        )
    return sniffed


def _display_filename(filename: str | None) -> str:
    """Keep the user's original name for display, minus any path component."""
    if not filename:
        return "upload"
    base = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return (base or "upload")[:255]


async def delete_document(db: AsyncSession, document: Document) -> None:
    """Delete the record and its stored blob.

    The row goes first: if blob deletion fails we have a harmless orphaned
    object, whereas the reverse order would leave a row whose file is gone.
    """
    key = document.storage_key
    await doc_repo.delete(db, document)
    try:
        await get_storage().delete(key)
    except Exception:
        logger.warning(
            "orphaned_blob_left_behind", extra={"storage_key": key}, exc_info=True
        )


async def ask_document(
    db: AsyncSession, *, document: Document, payload: AskRequest
) -> AskResponse:
    """Answer a natural-language question using only this document's text."""
    if document.status is not DocumentStatus.COMPLETED:
        raise DocumentNotReadyError(
            f"This document is '{document.status.value}'. Questions can only be "
            "asked once processing has completed successfully.",
            details={"status": document.status.value},
        )

    extraction = await doc_repo.get_extraction(db, document.id)
    if extraction is None or not extraction.raw_text.strip():
        raise DocumentNotReadyError(
            "No extracted text is stored for this document. Reprocess it and try again."
        )

    analyzer = get_analyzer()
    result = await analyzer.answer_question(
        extraction.raw_text, payload.question, filename=document.filename
    )

    return AskResponse(
        document_id=document.id,
        question=payload.question,
        answer=result.answer,
        answer_found=result.answer_found,
        quotes=result.quotes,
        provider=result.provider,
        model=result.model,
    )
