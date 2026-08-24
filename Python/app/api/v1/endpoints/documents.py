"""Document endpoints - upload, inspect, query, and reprocess.

The upload contract is **asynchronous by design**: `POST /documents` stores the
file, returns `202 Accepted` with a `pending` document, and the AI pipeline runs
in the background. Clients poll `GET /documents/{id}` until `status` becomes
`completed` or `failed`. See the module docstring of
:mod:`app.services.document_processor` for why the request does not block.
"""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, File, Response, UploadFile, status

from app.api.deps import (
    CurrentUser,
    DBSession,
    DocumentFilterParams,
    OwnedDocument,
    Pagination,
)
from app.core.exceptions import ConflictError, NotFoundError
from app.models.document import Document, DocumentStatus
from app.repositories import document as doc_repo
from app.schemas.common import ErrorResponse, Message, Page
from app.schemas.document import (
    TEXT_PREVIEW_CHARS,
    AskRequest,
    AskResponse,
    DocumentDetail,
    DocumentError,
    DocumentEventRead,
    DocumentOwner,
    DocumentRead,
    DocumentTextRead,
    ExtractionRead,
)
from app.services import document_service
from app.services.document_processor import schedule_processing
from app.services.storage import get_storage

router = APIRouter(prefix="/documents", tags=["Documents"])


# ---------------------------------------------------------------------- upload
@router.post(
    "",
    response_model=DocumentDetail,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a document for AI processing",
    responses={
        200: {
            "model": DocumentDetail,
            "description": "An identical file already existed; the original record is returned.",
        },
        413: {"model": ErrorResponse, "description": "File exceeds the size limit"},
        415: {"model": ErrorResponse, "description": "Unsupported file type"},
    },
)
async def upload_document(
    db: DBSession,
    current_user: CurrentUser,
    response: Response,
    file: UploadFile = File(..., description="PDF, PNG, JPEG, TIFF, or plain text."),
) -> DocumentDetail:
    """Upload a file and start the extraction + analysis pipeline.

    Returns **202 Accepted** immediately with `status: "pending"`. Poll
    `GET /documents/{id}` until the status is `completed` or `failed`; a completed
    document carries the full `extraction` payload.

    **Deduplication.** Files are fingerprinted with SHA-256 per user. Re-uploading
    a byte-identical file returns the *existing* record with **200 OK** instead of
    processing (and billing for) it twice.

    **Type checking.** The real format is detected from the file's magic bytes -
    a declared `Content-Type` is not trusted.
    """
    document, created = await document_service.upload_document(
        db, owner=current_user, upload=file
    )
    await db.commit()

    if not created:
        response.status_code = status.HTTP_200_OK
        return await _load_detail(db, document.id, owner_id=current_user.id)

    # Scheduled after the commit: the background task opens its own session and
    # would not see an uncommitted row.
    schedule_processing(document.id)
    return await _load_detail(db, document.id, owner_id=current_user.id)


# ------------------------------------------------------------------------ list
@router.get(
    "",
    response_model=Page[DocumentRead],
    summary="List your documents",
)
async def list_documents(
    db: DBSession,
    current_user: CurrentUser,
    pagination: Pagination,
    filters: DocumentFilterParams,
) -> Page[DocumentRead]:
    """Paginated list of your documents, newest first by default.

    Filter by `status` (`pending`/`processing`/`completed`/`failed`), by the
    AI-assigned `document_type` (`invoice`, `contract`, ...), or by a filename
    `search` substring. Sort with `sort_by` and `sort_dir`.

    Administrators see documents from **all** users.
    """
    documents, total = await doc_repo.list_documents(
        db,
        filters=filters,
        offset=pagination.offset,
        limit=pagination.limit,
        owner_id=None if current_user.is_admin else current_user.id,
    )
    return Page.build(
        [DocumentRead.model_validate(document) for document in documents],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.get(
    "/stats",
    response_model=dict[str, int],
    summary="Count your documents by status",
)
async def document_stats(db: DBSession, current_user: CurrentUser) -> dict[str, int]:
    """Return a `{status: count}` map - useful for a dashboard header.

    Computed with a single `GROUP BY`, not one query per status.
    """
    return await doc_repo.count_by_status(
        db, owner_id=None if current_user.is_admin else current_user.id
    )


# ---------------------------------------------------------------------- detail
@router.get(
    "/{document_id}",
    response_model=DocumentDetail,
    summary="Get a document with its AI analysis",
    responses={404: {"model": ErrorResponse, "description": "Document not found"}},
)
async def get_document(document: OwnedDocument) -> DocumentDetail:
    """Full detail for one document.

    While `status` is `pending` or `processing`, `extraction` is `null` - poll
    until it settles. When `status` is `failed`, `error` explains why. The
    `events` array is the processing audit trail.

    A document belonging to another user returns **404**, not 403, so ids cannot
    be probed for existence.
    """
    return _to_detail(document)


@router.get(
    "/{document_id}/text",
    response_model=DocumentTextRead,
    summary="Get the full extracted text",
    responses={
        404: {"model": ErrorResponse, "description": "Document not found"},
        409: {"model": ErrorResponse, "description": "Document has not been processed"},
    },
)
async def get_document_text(document: OwnedDocument, db: DBSession) -> DocumentTextRead:
    """Return the complete extracted text.

    Served separately from the detail view because it can be megabytes - the
    detail response carries only a short preview.
    """
    extraction = await doc_repo.get_extraction(db, document.id)
    if extraction is None:
        raise ConflictError(
            f"This document is '{document.status.value}' and has no extracted text yet.",
            code="document_not_ready",
        )
    return DocumentTextRead(
        document_id=document.id,
        text=extraction.raw_text,
        char_count=extraction.text_char_count,
        page_count=extraction.page_count,
        ocr_provider=extraction.ocr_provider,
    )


@router.get(
    "/{document_id}/download",
    summary="Download the original file",
    response_class=Response,
    responses={
        200: {"content": {"application/octet-stream": {}}, "description": "The original bytes"},
        404: {"model": ErrorResponse, "description": "Document or stored file not found"},
    },
)
async def download_document(document: OwnedDocument) -> Response:
    """Return the originally uploaded bytes."""
    data = await get_storage().load(document.storage_key)
    return Response(
        content=data,
        media_type=document.content_type,
        headers={
            # Both forms: `filename` for older clients, RFC 5987 `filename*` so
            # non-ASCII names survive. Quoting prevents header injection via a
            # crafted filename.
            "Content-Disposition": (
                f'attachment; filename="{_ascii_fallback(document.filename)}"; '
                f"filename*=UTF-8''{quote(document.filename)}"
            ),
            "Content-Length": str(len(data)),
        },
    )


# --------------------------------------------------------------------- actions
@router.post(
    "/{document_id}/ask",
    response_model=AskResponse,
    summary="Ask a question about a document",
    responses={
        404: {"model": ErrorResponse, "description": "Document not found"},
        409: {"model": ErrorResponse, "description": "Document has not completed processing"},
        502: {"model": ErrorResponse, "description": "The AI provider failed"},
    },
)
async def ask_document(
    payload: AskRequest, document: OwnedDocument, db: DBSession
) -> AskResponse:
    """Answer a natural-language question using only this document's text.

    The answer is **grounded**: the model is given the document text and
    instructed to use nothing else. When the document does not contain the answer,
    `answer_found` is `false` rather than a plausible invention, and `quotes`
    carries the verbatim passages the answer rests on.

    Requires `status: "completed"`.
    """
    return await document_service.ask_document(db, document=document, payload=payload)


@router.post(
    "/{document_id}/reprocess",
    response_model=DocumentDetail,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Re-run the AI pipeline",
    responses={
        404: {"model": ErrorResponse, "description": "Document not found"},
        409: {"model": ErrorResponse, "description": "Already queued or in progress"},
    },
)
async def reprocess_document(
    document: OwnedDocument, db: DBSession, current_user: CurrentUser
) -> DocumentDetail:
    """Queue the document for another pass through the pipeline.

    Use this to retry a `failed` document, or to re-analyse a `completed` one
    after configuring a better provider (for example after adding an OpenAI key).
    The previous extraction is replaced when the new run succeeds.

    Rejected with **409** if the document is already `pending` or `processing` -
    there is nothing to retry, and a second run would duplicate billable AI calls.
    """
    if document.status in (DocumentStatus.PENDING, DocumentStatus.PROCESSING):
        raise ConflictError(
            f"This document is already '{document.status.value}'.",
            code="already_queued",
            details={"status": document.status.value},
        )

    await doc_repo.reset_to_pending(db, document)
    await doc_repo.add_event(
        db,
        document.id,
        event="reprocess_requested",
        message=f"Requested by user {current_user.id}.",
    )
    await db.commit()

    schedule_processing(document.id)
    return await _load_detail(
        db, document.id, owner_id=None if current_user.is_admin else current_user.id
    )


@router.delete(
    "/{document_id}",
    response_model=Message,
    summary="Delete a document",
    responses={404: {"model": ErrorResponse, "description": "Document not found"}},
)
async def delete_document(document: OwnedDocument, db: DBSession) -> Message:
    """Delete the document, its extraction, its audit trail, and its stored file."""
    document_id = document.id
    await document_service.delete_document(db, document)
    await db.commit()
    return Message(detail=f"Document {document_id} deleted.")


# ------------------------------------------------------------------- assembly
async def _load_detail(db: DBSession, document_id: int, *, owner_id: int | None) -> DocumentDetail:
    """Re-read a document with its relationships eagerly loaded."""
    document = await doc_repo.get(db, document_id, owner_id=owner_id, with_details=True)
    if document is None:  # pragma: no cover - just written in this request
        raise NotFoundError(f"Document {document_id} was not found.")
    return _to_detail(document)


def _to_detail(document: Document) -> DocumentDetail:
    """Map the ORM aggregate onto the response schema.

    Done explicitly rather than by ``from_attributes`` alone because the text
    preview is derived and the error is a two-column pair collapsed into an
    object.
    """
    extraction = None
    if document.extraction is not None:
        record = document.extraction
        extraction = ExtractionRead(
            document_type=record.document_type,
            language=record.language,
            summary=record.summary,
            confidence=record.confidence,
            keywords=record.keywords or [],
            entities=record.entities or [],  # type: ignore[arg-type]
            fields=record.fields or [],  # type: ignore[arg-type]
            warnings=record.warnings or [],
            text_preview=_preview(record.raw_text),
            text_char_count=record.text_char_count,
            page_count=record.page_count,
            ocr_provider=record.ocr_provider,
            ocr_duration_ms=record.ocr_duration_ms,
            analysis_provider=record.analysis_provider,
            analysis_model=record.analysis_model,
            analysis_duration_ms=record.analysis_duration_ms,
            prompt_tokens=record.prompt_tokens,
            completion_tokens=record.completion_tokens,
        )

    error = None
    if document.error_code:
        error = DocumentError(
            code=document.error_code,
            message=document.error_message or "Processing failed.",
        )

    return DocumentDetail(
        id=document.id,
        filename=document.filename,
        content_type=document.content_type,
        size_bytes=document.size_bytes,
        checksum_sha256=document.checksum_sha256,
        status=document.status,
        document_type=document.document_type,
        page_count=document.page_count,
        attempt_count=document.attempt_count,
        processing_duration_ms=document.processing_duration_ms,
        created_at=document.created_at,
        updated_at=document.updated_at,
        owner=DocumentOwner.model_validate(document.owner),
        extraction=extraction,
        error=error,
        events=[DocumentEventRead.model_validate(event) for event in document.events],
    )


def _preview(text: str) -> str:
    if len(text) <= TEXT_PREVIEW_CHARS:
        return text
    return text[:TEXT_PREVIEW_CHARS].rstrip() + "..."


def _ascii_fallback(filename: str) -> str:
    """ASCII-only, quote-free filename for the legacy Content-Disposition form."""
    cleaned = filename.encode("ascii", "ignore").decode().replace('"', "").replace("\\", "")
    return cleaned.strip() or "document"
