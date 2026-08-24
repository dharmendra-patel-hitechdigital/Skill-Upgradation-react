"""Schemas for the document-processing feature.

:class:`DocumentAnalysis` is deliberately shared between two consumers:

* it is the **response contract** for the API, and
* it is the **output contract** for the LLM - its JSON Schema is handed to
  OpenAI's structured-output mode, so the model is constrained to return
  exactly these keys and types.

Keeping one definition means the API can never drift from what the AI layer
actually produces.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.document import DocumentStatus

# Full raw text can be megabytes; the detail response carries a preview and the
# complete text is served by GET /documents/{id}/text.
TEXT_PREVIEW_CHARS = 1_500


class EntityType(StrEnum):
    """Closed vocabulary for entities, so clients can switch on the value."""

    PERSON = "person"
    ORGANIZATION = "organization"
    LOCATION = "location"
    DATE = "date"
    MONEY = "money"
    EMAIL = "email"
    PHONE = "phone"
    IDENTIFIER = "identifier"
    OTHER = "other"


class DocumentKind(StrEnum):
    """Closed vocabulary for document classification."""

    INVOICE = "invoice"
    RECEIPT = "receipt"
    CONTRACT = "contract"
    RESUME = "resume"
    REPORT = "report"
    LETTER = "letter"
    FORM = "form"
    IDENTITY = "identity_document"
    STATEMENT = "bank_statement"
    OTHER = "other"


class ExtractedEntity(BaseModel):
    """A named thing found in the document."""

    text: str = Field(max_length=512, examples=["Acme Corporation"])
    type: EntityType = Field(examples=[EntityType.ORGANIZATION])
    confidence: float | None = Field(default=None, ge=0.0, le=1.0, examples=[0.94])


class ExtractedField(BaseModel):
    """A key/value pair pulled out of the document (invoice total, due date...)."""

    key: str = Field(max_length=128, examples=["invoice_total"])
    value: str | None = Field(default=None, max_length=2048, examples=["1240.50"])
    confidence: float | None = Field(default=None, ge=0.0, le=1.0, examples=[0.88])


class DocumentAnalysis(BaseModel):
    """Structured understanding of a document - the LLM's output contract."""

    document_type: DocumentKind = Field(
        description="Best-guess classification of the document."
    )
    language: str | None = Field(
        default=None,
        max_length=16,
        description="ISO 639-1 code of the dominant language.",
        examples=["en"],
    )
    summary: str = Field(
        max_length=4000, description="Two to four sentences describing the document."
    )
    keywords: list[str] = Field(
        default_factory=list, max_length=25, description="Salient terms or topics."
    )
    entities: list[ExtractedEntity] = Field(default_factory=list, max_length=100)
    fields: list[ExtractedField] = Field(default_factory=list, max_length=100)
    confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="The analyser's own confidence in this result.",
    )
    warnings: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Caveats, e.g. truncated input or an unreadable page.",
    )

    @field_validator("keywords", mode="before")
    @classmethod
    def _clean_keywords(cls, value: object) -> object:
        """Trim, de-duplicate (case-insensitively) and drop empties."""
        if not isinstance(value, list):
            return value
        seen: set[str] = set()
        cleaned: list[str] = []
        for item in value:
            if not isinstance(item, str):
                continue
            text = item.strip()
            key = text.lower()
            if text and key not in seen:
                seen.add(key)
                cleaned.append(text)
        return cleaned


class ExtractionRead(BaseModel):
    """The persisted result of one pipeline run, as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    document_type: str | None
    language: str | None
    summary: str | None
    confidence: float | None
    keywords: list[str]
    entities: list[ExtractedEntity]
    fields: list[ExtractedField]
    warnings: list[str]

    text_preview: str = Field(
        description=(
            f"First {TEXT_PREVIEW_CHARS} characters of the extracted text. "
            "Use GET /documents/{id}/text for the whole thing."
        )
    )
    text_char_count: int
    page_count: int | None

    # Provider provenance - which engine produced this, and what it cost.
    ocr_provider: str
    ocr_duration_ms: int | None
    analysis_provider: str
    analysis_model: str | None
    analysis_duration_ms: int | None
    prompt_tokens: int | None
    completion_tokens: int | None


class DocumentEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event: str
    message: str | None
    created_at: datetime


class DocumentError(BaseModel):
    code: str
    message: str


class DocumentOwner(BaseModel):
    """Who uploaded a document.

    Always returned, for both roles. That leaks nothing: a regular user only
    ever receives their *own* documents, so this is their own identity echoed
    back - while an administrator listing the whole installation genuinely
    cannot use the list without it.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    full_name: str | None = None


class DocumentRead(BaseModel):
    """List-view representation: metadata and lifecycle, no heavy payload."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    filename: str
    content_type: str
    size_bytes: int
    checksum_sha256: str
    status: DocumentStatus
    document_type: str | None
    page_count: int | None
    attempt_count: int
    processing_duration_ms: int | None
    created_at: datetime
    updated_at: datetime
    owner: DocumentOwner


class DocumentDetail(DocumentRead):
    """Detail view: adds the extraction result, the error, and the audit trail."""

    extraction: ExtractionRead | None = None
    error: DocumentError | None = None
    events: list[DocumentEventRead] = Field(default_factory=list)


class DocumentTextRead(BaseModel):
    """Full extracted text for a document."""

    document_id: int
    text: str
    char_count: int
    page_count: int | None
    ocr_provider: str


class DocumentSortField(StrEnum):
    CREATED_AT = "created_at"
    UPDATED_AT = "updated_at"
    FILENAME = "filename"
    SIZE_BYTES = "size_bytes"


class SortDirection(StrEnum):
    ASC = "asc"
    DESC = "desc"


class DocumentFilters(BaseModel):
    """Query parameters for the document list endpoint."""

    status: DocumentStatus | None = Field(
        default=None, description="Only documents in this processing state."
    )
    document_type: str | None = Field(
        default=None, max_length=64, description="Only this AI-assigned type."
    )
    search: str | None = Field(
        default=None, max_length=255, description="Case-insensitive filename substring."
    )
    owner_email: str | None = Field(
        default=None,
        max_length=255,
        description=(
            "Case-insensitive substring of the uploader's email. Only meaningful "
            "for an administrator - a regular user's list is already restricted "
            "to their own documents, so this can never widen it."
        ),
    )
    sort_by: DocumentSortField = DocumentSortField.CREATED_AT
    sort_dir: SortDirection = SortDirection.DESC


class AskRequest(BaseModel):
    """Ask a natural-language question about one processed document."""

    question: Annotated[str, Field(min_length=3, max_length=1000)] = Field(
        examples=["What is the total amount due and when is it payable?"]
    )

    @field_validator("question")
    @classmethod
    def _strip_question(cls, value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) < 3:
            raise ValueError("Question is too short.")
        return cleaned


class AskResponse(BaseModel):
    """Answer to an :class:`AskRequest`, grounded in the document's text."""

    document_id: int
    question: str
    answer: str
    # False when the document simply does not contain the answer. Surfacing this
    # explicitly is what stops the feature from being a hallucination machine.
    answer_found: bool = Field(
        description="False when the document does not contain the answer."
    )
    quotes: list[str] = Field(
        default_factory=list,
        description="Verbatim snippets from the document supporting the answer.",
    )
    provider: str
    model: str | None = None


class ProcessingOptions(BaseModel):
    """Optional per-request overrides for a reprocess call."""

    force: bool = Field(
        default=False,
        description="Reprocess even if the document already completed successfully.",
    )
