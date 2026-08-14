"""The document-processing pipeline - the heart of the AI feature.

Flow
----
``upload`` returns as soon as the file is stored and a row exists (HTTP 202).
Processing then runs out-of-band:

    stage 0  claim      atomic PENDING -> PROCESSING (one worker wins)
    stage 1  fetch      read the bytes back from storage
    stage 2  extract    bytes -> text        (local PDF reader | AWS Textract)
    stage 3  analyse    text  -> structure   (OpenAI | rule-based engine)
    stage 4  persist    write the extraction, mark COMPLETED
             on error   mark FAILED with a code and an actionable message

Why the request does not wait
-----------------------------
OCR plus an LLM call is seconds to minutes. Holding an HTTP connection open for
that long means client timeouts, retries that duplicate billable work, and a
request that cannot survive a deploy. Returning 202 with a polling URL is the
correct shape for this workload, and it is what lets the same pipeline later run
on a queue without any API change.

Transaction discipline
----------------------
Three short transactions, not one long one. The slow work in stages 1-3 happens
with **no transaction open**, because holding one across a 60-second network call
pins a pooled connection, blocks other writers, and risks the database killing an
idle-in-transaction session. Each database phase opens, commits, and closes.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from app.core.config import settings
from app.core.database import session_scope
from app.core.exceptions import (
    AIProviderError,
    AppError,
    ConflictError,
    ExtractionError,
    StorageError,
)
from app.core.logging import safe_extra
from app.models.document import DocumentStatus
from app.repositories import document as doc_repo
from app.schemas.document import DocumentAnalysis
from app.services.ai.base import AnalysisResult, TextExtractionResult
from app.services.ai.registry import get_analyzer_with_fallback, get_text_extractor
from app.services.storage import get_storage
from app.services.task_runner import task_runner

logger = logging.getLogger(__name__)

# Which transitions the service will perform. Kept as data so the rule is
# checkable and testable rather than scattered across if-statements.
_ALLOWED_TRANSITIONS: dict[DocumentStatus, frozenset[DocumentStatus]] = {
    DocumentStatus.PENDING: frozenset({DocumentStatus.PROCESSING}),
    DocumentStatus.PROCESSING: frozenset(
        {DocumentStatus.COMPLETED, DocumentStatus.FAILED}
    ),
    DocumentStatus.COMPLETED: frozenset({DocumentStatus.PENDING}),
    DocumentStatus.FAILED: frozenset({DocumentStatus.PENDING}),
}


def can_transition(current: DocumentStatus, target: DocumentStatus) -> bool:
    return target in _ALLOWED_TRANSITIONS.get(current, frozenset())


def assert_can_transition(current: DocumentStatus, target: DocumentStatus) -> None:
    """Raise a domain error for an illegal lifecycle move."""
    if not can_transition(current, target):
        raise ConflictError(
            f"A document with status '{current.value}' cannot move to "
            f"'{target.value}'.",
            code="invalid_transition",
            details={"from": current.value, "to": target.value},
        )


@dataclass(frozen=True, slots=True)
class _Job:
    """Everything stage 1-3 needs, read once and detached from any session.

    Passing plain values (rather than an ORM instance) guarantees the slow
    section cannot trigger a lazy load against a closed session.
    """

    document_id: int
    owner_id: int
    filename: str
    content_type: str
    storage_key: str
    size_bytes: int


def schedule_processing(document_id: int) -> None:
    """Queue a document for processing on the shared background runner."""
    task_runner.submit(
        lambda: process_document(document_id),
        name=f"process-document-{document_id}",
    )


async def process_document(document_id: int) -> None:
    """Run the full pipeline for one document. Never raises.

    Every failure is recorded on the document as a status plus an error code, so
    the API can always explain what happened. Letting an exception escape would
    only reach the task runner's logger and leave the row stuck in PROCESSING.
    """
    job = await _claim(document_id)
    if job is None:
        return

    # safe_extra() because "filename" is a reserved LogRecord attribute - passing
    # it raw makes logging itself raise, and this dict is used on the error paths
    # where that failure would be most damaging.
    log_context = safe_extra({"document_id": document_id, "filename": job.filename})

    try:
        async with asyncio.timeout(settings.PROCESSING_TIMEOUT_SECONDS):
            data = await _fetch_bytes(job)
            extraction = await _extract_text(job, data)
            analysis = await _analyse(job, extraction)
    except TimeoutError:
        logger.warning("document_processing_timeout", extra=log_context)
        await _fail(
            document_id,
            code="processing_timeout",
            message=(
                "Processing exceeded the "
                f"{settings.PROCESSING_TIMEOUT_SECONDS:.0f}s limit. Try a smaller "
                "document, or raise PROCESSING_TIMEOUT_SECONDS."
            ),
        )
        return
    except AppError as exc:
        # Expected, classified failure - an unreadable scan, a dead provider, a
        # missing file. Recorded verbatim because the message is user-facing.
        logger.info(
            "document_processing_failed",
            extra={**log_context, "error_code": exc.code, "reason": exc.message},
        )
        await _fail(document_id, code=exc.code, message=exc.message)
        return
    except asyncio.CancelledError:
        # Shutdown. Leave the row in PROCESSING: the startup sweep will reclaim
        # it, and swallowing cancellation here would break task cancellation.
        logger.warning("document_processing_cancelled", extra=log_context)
        raise
    except Exception as exc:
        logger.exception("document_processing_crashed", extra=log_context)
        await _fail(
            document_id,
            code="internal_error",
            message=f"An unexpected error occurred while processing: {type(exc).__name__}",
        )
        return

    await _persist(job, extraction, analysis)
    logger.info(
        "document_processed",
        extra={
            **log_context,
            "ocr_provider": extraction.provider,
            "analysis_provider": analysis.provider,
            "chars": extraction.char_count,
            "document_type": analysis.analysis.document_type.value,
        },
    )


# ----------------------------------------------------------------- stage 0: claim
async def _claim(document_id: int) -> _Job | None:
    """Win the race to process this document, and snapshot what we need."""
    async with session_scope() as db:
        won = await doc_repo.claim_for_processing(db, document_id)
        if not won:
            # Either another worker got there first, or the row is not PENDING.
            document = await doc_repo.get(db, document_id)
            logger.info(
                "document_claim_skipped",
                extra={
                    "document_id": document_id,
                    "status": document.status.value if document else "missing",
                },
            )
            return None

        document = await doc_repo.get(db, document_id)
        if document is None:  # pragma: no cover - deleted between statements
            return None

        await doc_repo.add_event(
            db, document_id, event="processing_started", message="Pipeline claimed the document."
        )
        return _Job(
            document_id=document.id,
            owner_id=document.owner_id,
            filename=document.filename,
            content_type=document.content_type,
            storage_key=document.storage_key,
            size_bytes=document.size_bytes,
        )


# ----------------------------------------------------------------- stage 1: fetch
async def _fetch_bytes(job: _Job) -> bytes:
    try:
        return await get_storage().load(job.storage_key)
    except StorageError as exc:
        raise StorageError(
            "The uploaded file could not be read back from storage. It may have "
            "been removed.",
            details={"storage_key": job.storage_key},
        ) from exc


# --------------------------------------------------------------- stage 2: extract
async def _extract_text(job: _Job, data: bytes) -> TextExtractionResult:
    extractor = get_text_extractor(job.content_type)
    result = await extractor.extract(
        data, content_type=job.content_type, filename=job.filename
    )
    if not result.has_text:
        raise ExtractionError(
            "The document produced no readable text.",
            details={"provider": result.provider},
        )
    return result


# --------------------------------------------------------------- stage 3: analyse
async def _analyse(job: _Job, extraction: TextExtractionResult) -> AnalysisResult:
    """Analyse the text, falling back to the rule engine if the LLM fails.

    A provider outage should degrade the *quality* of a result, not lose the
    document. The fallback's output is tagged with a warning so a consumer can
    tell a rule-based analysis from a model-generated one.
    """
    primary, fallback = get_analyzer_with_fallback()
    text = _merge_detected_fields(extraction)

    try:
        result = await primary.analyze(
            text, filename=job.filename, content_type=job.content_type
        )
    except AIProviderError as exc:
        if fallback is None:
            raise
        logger.warning(
            "analysis_fallback_engaged",
            extra={
                "document_id": job.document_id,
                "primary": primary.name,
                "fallback": fallback.name,
                "reason": exc.message,
            },
        )
        result = await fallback.analyze(
            text, filename=job.filename, content_type=job.content_type
        )
        result.analysis.warnings.append(
            f"The primary analyser ({primary.name}) was unavailable: {exc.message} "
            f"This result came from the {fallback.name} engine."
        )

    _merge_extractor_fields(result.analysis, extraction)
    return result


def _merge_detected_fields(extraction: TextExtractionResult) -> str:
    """Prepend OCR-detected form fields to the text handed to the analyser.

    Textract's FORMS output encodes spatial relationships (this label belongs to
    that box) that vanish once text is flattened. Showing the analyser those
    pairs up front measurably improves field extraction on real forms.
    """
    if not extraction.detected_fields:
        return extraction.text

    lines = "\n".join(
        f"{pair.get('key', '')}: {pair.get('value', '')}"
        for pair in extraction.detected_fields[:60]
        if pair.get("key")
    )
    if not lines:
        return extraction.text
    return (
        "--- FORM FIELDS DETECTED BY OCR ---\n"
        f"{lines}\n"
        "--- FULL DOCUMENT TEXT ---\n"
        f"{extraction.text}"
    )


def _merge_extractor_fields(
    analysis: DocumentAnalysis, extraction: TextExtractionResult
) -> None:
    """Add OCR-detected pairs the analyser did not already report."""
    if not extraction.detected_fields:
        return

    from app.schemas.document import ExtractedField

    known = {field.key.lower() for field in analysis.fields}
    for pair in extraction.detected_fields:
        raw_key = (pair.get("key") or "").strip()
        if not raw_key:
            continue
        key = _snake(raw_key)
        if key in known:
            continue
        known.add(key)
        analysis.fields.append(
            ExtractedField(
                key=key,
                value=(pair.get("value") or "").strip()[:2048] or None,
                confidence=0.8,
            )
        )
        if len(analysis.fields) >= 100:
            break


def _snake(label: str) -> str:
    import re

    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", label.strip().lower()).strip("_")
    return (cleaned or "field")[:128]


# --------------------------------------------------------------- stage 4: persist
async def _persist(
    job: _Job, extraction: TextExtractionResult, analysis: AnalysisResult
) -> None:
    result = analysis.analysis
    warnings = list(dict.fromkeys([*extraction.warnings, *result.warnings]))

    async with session_scope() as db:
        await doc_repo.upsert_extraction(
            db,
            job.document_id,
            {
                "raw_text": extraction.text,
                "text_char_count": extraction.char_count,
                "page_count": extraction.page_count,
                "ocr_provider": extraction.provider,
                "ocr_duration_ms": extraction.duration_ms,
                "analysis_provider": analysis.provider,
                "analysis_model": analysis.model,
                "analysis_duration_ms": analysis.duration_ms,
                "prompt_tokens": analysis.prompt_tokens,
                "completion_tokens": analysis.completion_tokens,
                "document_type": result.document_type.value,
                "language": result.language,
                "summary": result.summary,
                "confidence": result.confidence,
                "keywords": result.keywords,
                # model_dump(mode="json") so enums become strings the JSON column
                # can serialise.
                "entities": [e.model_dump(mode="json") for e in result.entities],
                "fields": [f.model_dump(mode="json") for f in result.fields],
                "warnings": warnings,
            },
        )
        await doc_repo.mark_completed(
            db,
            job.document_id,
            document_type=result.document_type.value,
            page_count=extraction.page_count,
        )
        await doc_repo.add_event(
            db,
            job.document_id,
            event="processing_completed",
            message=(
                f"Extracted {extraction.char_count} characters and classified as "
                f"'{result.document_type.value}'."
            ),
            payload={
                "ocr_provider": extraction.provider,
                "analysis_provider": analysis.provider,
                "analysis_model": analysis.model,
                "prompt_tokens": analysis.prompt_tokens,
                "completion_tokens": analysis.completion_tokens,
                "confidence": result.confidence,
            },
        )


async def _fail(document_id: int, *, code: str, message: str) -> None:
    """Record a failure. Swallows its own errors - it is the last line of defence."""
    try:
        async with session_scope() as db:
            await doc_repo.mark_failed(db, document_id, code=code, message=message)
            await doc_repo.add_event(
                db, document_id, event="processing_failed", message=message,
                payload={"error_code": code},
            )
    except Exception:  # pragma: no cover - database itself is down
        logger.exception(
            "failed_to_record_failure", extra={"document_id": document_id}
        )


# -------------------------------------------------------------------- recovery
async def recover_stuck_documents() -> int:
    """Return abandoned PROCESSING documents to FAILED. Called on startup.

    Background work lives in this process, so a crash or a deploy mid-pipeline
    leaves rows in PROCESSING with nothing running. Without this sweep those
    documents would poll forever. Marking them FAILED (not PENDING) is
    deliberate: an automatic retry could re-run a half-billed AI call on every
    restart loop, so a human or an explicit API call decides to retry.
    """
    reclaimed = 0
    async with session_scope() as db:
        stuck = await doc_repo.list_stuck_processing(
            db, older_than_seconds=settings.PROCESSING_TIMEOUT_SECONDS
        )
        for document in stuck:
            await doc_repo.mark_failed(
                db,
                document.id,
                code="worker_interrupted",
                message=(
                    "Processing was interrupted by a server restart. "
                    "Reprocess the document to try again."
                ),
            )
            await doc_repo.add_event(
                db,
                document.id,
                event="processing_interrupted",
                message="Reclaimed after a server restart.",
            )
            reclaimed += 1

    if reclaimed:
        logger.warning("reclaimed_stuck_documents", extra={"count": reclaimed})
    return reclaimed
