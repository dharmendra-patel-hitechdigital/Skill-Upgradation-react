"""AWS Textract adapter - real OCR for scans, photos, and multi-page PDFs.

Textract exposes two families of API, and picking the wrong one is the usual
source of "it worked on my one-page test and broke in production":

* **Synchronous** (``AnalyzeDocument``): accepts raw bytes, returns immediately,
  but is limited to a *single page* and ~5 MB. Perfect for a photographed
  receipt or an ID card.
* **Asynchronous** (``StartDocumentAnalysis`` + polling): the only way to process
  a multi-page PDF, and it reads exclusively from S3 - you cannot hand it bytes.

This adapter routes between them automatically and, when the async path is
needed, stages the file into S3 itself and cleans up afterwards. Callers just
call :meth:`extract`.

``FORMS`` is requested alongside raw text because Textract's key/value detection
uses the document's actual geometry - label-to-the-left-of-box, table headers -
which is information irrecoverably lost once text is flattened into a string. We
pass those pairs downstream as ``detected_fields`` so the analysis stage starts
from OCR ground truth rather than re-guessing from prose.

Note on verification: the code paths here require live AWS credentials, so they
are exercised against a stubbed boto3 client in the test suite rather than the
real service. The default (local) engine is covered end-to-end.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any

import anyio

from app.core.config import settings
from app.core.exceptions import AIProviderError, ExtractionError
from app.services.ai.base import TextExtractionResult

logger = logging.getLogger(__name__)

SUPPORTED_TYPES = frozenset(
    {
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/jpg",
        "image/tiff",
    }
)

# Textract async jobs: how aggressively to poll. Starts tight so small documents
# finish fast, backs off so a slow job does not hammer the API (and its quota).
_POLL_INITIAL_SECONDS = 1.0
_POLL_MAX_SECONDS = 8.0
_POLL_BACKOFF = 1.5


class TextractExtractor:
    """OCR via AWS Textract, with automatic sync/async routing."""

    name = "textract"

    def __init__(self, *, region: str | None = None, staging_bucket: str | None = None) -> None:
        self._region = region or settings.AWS_REGION
        self._staging_bucket = staging_bucket
        self._client: Any = None
        self._s3_client: Any = None

    # ------------------------------------------------------------------ clients
    def _textract(self) -> Any:
        if self._client is None:
            self._client = self._make_client("textract")
        return self._client

    def _s3(self) -> Any:
        if self._s3_client is None:
            self._s3_client = self._make_client("s3")
        return self._s3_client

    def _make_client(self, service: str) -> Any:
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - boto3 is a hard dep
            raise AIProviderError("boto3 is not installed.") from exc

        kwargs: dict[str, Any] = {"region_name": self._region}
        if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY:
            kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY_ID
            kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY
        return boto3.client(service, **kwargs)

    # -------------------------------------------------------------------- api
    def supports(self, content_type: str) -> bool:
        return content_type.split(";")[0].strip().lower() in SUPPORTED_TYPES

    async def extract(
        self, data: bytes, *, content_type: str, filename: str
    ) -> TextExtractionResult:
        base = content_type.split(";")[0].strip().lower()
        if base not in SUPPORTED_TYPES:
            raise ExtractionError(
                f"Textract does not support '{content_type}'.",
                details={"content_type": content_type},
            )

        started = time.perf_counter()
        needs_async = base == "application/pdf" and len(data) > settings.TEXTRACT_MAX_SYNC_BYTES

        if needs_async:
            if not self._staging_bucket:
                raise ExtractionError(
                    "This PDF is too large for Textract's synchronous API and no S3 "
                    "bucket is configured for the asynchronous one. Set S3_BUCKET "
                    "(and STORAGE_BACKEND=s3) to process large documents.",
                    details={"size_bytes": len(data)},
                )
            blocks, warnings = await self._analyze_via_s3(data, filename=filename)
        else:
            blocks, warnings = await self._analyze_sync(data)

        text, page_count = _lines_to_text(blocks)
        fields = _extract_key_values(blocks)
        duration_ms = int((time.perf_counter() - started) * 1000)

        if not text.strip():
            raise ExtractionError(
                "Textract returned no text for this document.",
                details={"filename": filename},
            )

        return TextExtractionResult(
            text=text,
            provider=self.name,
            page_count=page_count,
            duration_ms=duration_ms,
            warnings=warnings,
            detected_fields=fields,
        )

    # ---------------------------------------------------------- sync (1 page)
    async def _analyze_sync(self, data: bytes) -> tuple[list[dict[str, Any]], list[str]]:
        client = self._textract()

        def _call() -> dict[str, Any]:
            return client.analyze_document(
                Document={"Bytes": data}, FeatureTypes=["FORMS", "TABLES"]
            )

        try:
            response = await anyio.to_thread.run_sync(_call)
        except Exception as exc:
            raise _translate_error(exc) from exc
        return list(response.get("Blocks", [])), []

    # -------------------------------------------------- async (multi-page PDF)
    async def _analyze_via_s3(
        self, data: bytes, *, filename: str
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Stage the file in S3, run an async job, collect every page, clean up."""
        key = f"textract-staging/{uuid.uuid4().hex}"
        s3 = self._s3()
        textract = self._textract()
        bucket = self._staging_bucket
        assert bucket is not None

        def _upload() -> None:
            s3.put_object(Bucket=bucket, Key=key, Body=data)

        def _start() -> str:
            response = textract.start_document_analysis(
                DocumentLocation={"S3Object": {"Bucket": bucket, "Name": key}},
                FeatureTypes=["FORMS", "TABLES"],
            )
            return str(response["JobId"])

        def _cleanup() -> None:
            try:
                s3.delete_object(Bucket=bucket, Key=key)
            except Exception:  # pragma: no cover - best effort
                logger.warning("textract_staging_cleanup_failed", extra={"key": key})

        try:
            await anyio.to_thread.run_sync(_upload)
            job_id = await anyio.to_thread.run_sync(_start)
            logger.info("textract_job_started", extra={"job_id": job_id, "file": filename})
            return await self._collect_job(job_id)
        except Exception as exc:
            raise _translate_error(exc) from exc
        finally:
            await anyio.to_thread.run_sync(_cleanup)

    async def _collect_job(self, job_id: str) -> tuple[list[dict[str, Any]], list[str]]:
        client = self._textract()
        deadline = time.monotonic() + settings.PROCESSING_TIMEOUT_SECONDS
        delay = _POLL_INITIAL_SECONDS

        def _get(token: str | None) -> dict[str, Any]:
            kwargs: dict[str, Any] = {"JobId": job_id}
            if token:
                kwargs["NextToken"] = token
            return client.get_document_analysis(**kwargs)

        # Phase 1: wait for the job to leave IN_PROGRESS.
        while True:
            response = await anyio.to_thread.run_sync(_get, None)
            status = response.get("JobStatus")

            if status == "SUCCEEDED":
                break
            if status in ("FAILED", "PARTIAL_SUCCESS"):
                message = response.get("StatusMessage") or "Textract reported a failure."
                if status == "FAILED":
                    raise ExtractionError(f"Textract could not process the document: {message}")
                break  # PARTIAL_SUCCESS - keep whatever pages did work
            if time.monotonic() > deadline:
                raise ExtractionError(
                    "Textract did not finish within the processing timeout.",
                    details={"job_id": job_id},
                )
            await anyio.sleep(delay)
            delay = min(delay * _POLL_BACKOFF, _POLL_MAX_SECONDS)

        # Phase 2: drain every page of results.
        warnings: list[str] = []
        if response.get("JobStatus") == "PARTIAL_SUCCESS":
            warnings.append("Textract processed only part of this document.")

        blocks = list(response.get("Blocks", []))
        token = response.get("NextToken")
        while token:
            if time.monotonic() > deadline:
                warnings.append("Result pagination timed out; output may be incomplete.")
                break
            page = await anyio.to_thread.run_sync(_get, token)
            blocks.extend(page.get("Blocks", []))
            token = page.get("NextToken")

        return blocks, warnings


# ----------------------------------------------------------------- block parsing
def _lines_to_text(blocks: list[dict[str, Any]]) -> tuple[str, int | None]:
    """Rebuild readable text from Textract LINE blocks, grouped by page."""
    pages: dict[int, list[str]] = {}
    for block in blocks:
        if block.get("BlockType") != "LINE":
            continue
        page_no = int(block.get("Page", 1))
        text = block.get("Text", "")
        if text:
            pages.setdefault(page_no, []).append(text)

    if not pages:
        return "", None

    body = "\n\n".join(
        f"[page {page_no}]\n" + "\n".join(lines) for page_no, lines in sorted(pages.items())
    )
    return body, max(pages)


def _extract_key_values(blocks: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Turn Textract's FORMS output into flat ``{key, value}`` pairs.

    Textract models a form field as a KEY block linked to a VALUE block, each of
    which links to the WORD blocks holding the actual text - so reconstructing a
    pair means walking two levels of relationships through a block index.
    """
    by_id = {block["Id"]: block for block in blocks if "Id" in block}
    pairs: list[dict[str, str]] = []

    for block in blocks:
        if block.get("BlockType") != "KEY_VALUE_SET":
            continue
        if "KEY" not in block.get("EntityTypes", []):
            continue

        key_text = _words_for(block, by_id)
        value_text = ""
        for relationship in block.get("Relationships", []):
            if relationship.get("Type") != "VALUE":
                continue
            for value_id in relationship.get("Ids", []):
                value_block = by_id.get(value_id)
                if value_block:
                    value_text = _words_for(value_block, by_id)

        key_clean = key_text.strip().rstrip(":").strip()
        if key_clean:
            pairs.append({"key": key_clean, "value": value_text.strip()})

    return pairs


def _words_for(block: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> str:
    """Concatenate the WORD/SELECTION_ELEMENT children of a block."""
    words: list[str] = []
    for relationship in block.get("Relationships", []):
        if relationship.get("Type") != "CHILD":
            continue
        for child_id in relationship.get("Ids", []):
            child = by_id.get(child_id)
            if not child:
                continue
            if child.get("BlockType") == "WORD":
                words.append(child.get("Text", ""))
            # Checkboxes carry no text - render the state so a ticked box is not
            # silently dropped from the extracted fields.
            elif (
                child.get("BlockType") == "SELECTION_ELEMENT"
                and child.get("SelectionStatus") == "SELECTED"
            ):
                words.append("[x]")
    return " ".join(word for word in words if word)


def _translate_error(exc: Exception) -> Exception:
    """Map botocore errors onto domain errors with actionable messages."""
    name = type(exc).__name__
    text = str(exc)

    if name == "ClientError":
        code = ""
        response = getattr(exc, "response", None)
        if isinstance(response, dict):
            code = str(response.get("Error", {}).get("Code", ""))

        if code in ("UnsupportedDocumentException", "BadDocumentException"):
            return ExtractionError(
                "Textract rejected this file as unreadable or unsupported.",
                details={"aws_code": code},
            )
        if code in ("DocumentTooLargeException",):
            return ExtractionError(
                "The document exceeds Textract's size limit.", details={"aws_code": code}
            )
        if code in (
            "AccessDeniedException",
            "UnrecognizedClientException",
            "InvalidSignatureException",
        ):
            return AIProviderError(
                "AWS rejected the Textract credentials. Check the IAM role or keys.",
                details={"aws_code": code},
            )
        if code in ("ProvisionedThroughputExceededException", "ThrottlingException"):
            return AIProviderError(
                "Textract is throttling requests; retry this document shortly.",
                details={"aws_code": code},
            )
        return AIProviderError(f"Textract call failed: {text}", details={"aws_code": code})

    if name in ("NoCredentialsError", "PartialCredentialsError"):
        return AIProviderError(
            "No AWS credentials were found for Textract. Configure an IAM role or "
            "set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY."
        )
    if name == "EndpointConnectionError":
        return AIProviderError("Could not reach the AWS Textract endpoint.")

    return AIProviderError(f"Unexpected Textract failure: {text}")
