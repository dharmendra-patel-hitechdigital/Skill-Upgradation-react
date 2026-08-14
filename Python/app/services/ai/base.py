"""Provider-agnostic contracts for the AI layer.

The document pipeline depends only on the two protocols below - never on
``openai`` or ``boto3``. Consequences worth stating out loud, because they are
the whole point of the indirection:

* **The feature works with zero third-party credentials.** A pure-Python
  extractor and a rule-based analyser implement the same protocols, so the
  service is fully functional (and demonstrable) before any account exists.
* **Providers are swappable per environment.** Textract in production, local
  parsing in CI, and no test needs a network call or a mock of someone else's
  SDK - just a different implementation of the protocol.
* **Every result carries its own provenance.** Which engine ran, how long it
  took, how many tokens it burned. That is what makes cost and latency
  attributable per document instead of a single opaque monthly bill.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from app.schemas.document import DocumentAnalysis


@dataclass(slots=True)
class TextExtractionResult:
    """Output of stage 1 - getting characters out of a file."""

    text: str
    provider: str
    page_count: int | None = None
    duration_ms: int | None = None
    warnings: list[str] = field(default_factory=list)
    # Some engines (notably Textract FORMS) return key/value pairs directly.
    # Passing them downstream lets the analyser use real OCR geometry instead of
    # re-deriving fields from flattened text.
    detected_fields: list[dict[str, str]] = field(default_factory=list)

    @property
    def char_count(self) -> int:
        return len(self.text)

    @property
    def has_text(self) -> bool:
        return bool(self.text.strip())


@dataclass(slots=True)
class AnalysisResult:
    """Output of stage 2 - turning text into structured understanding."""

    analysis: DocumentAnalysis
    provider: str
    model: str | None = None
    duration_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


@dataclass(slots=True)
class AnswerResult:
    """Output of the question-answering path."""

    answer: str
    answer_found: bool
    quotes: list[str] = field(default_factory=list)
    provider: str = "unknown"
    model: str | None = None


@runtime_checkable
class TextExtractor(Protocol):
    """Stage 1: bytes -> text."""

    name: str

    def supports(self, content_type: str) -> bool:
        """Whether this engine can handle the given MIME type."""
        ...

    async def extract(
        self, data: bytes, *, content_type: str, filename: str
    ) -> TextExtractionResult: ...


@runtime_checkable
class DocumentAnalyzer(Protocol):
    """Stage 2: text -> structure, and text + question -> answer."""

    name: str
    model: str | None

    async def analyze(
        self, text: str, *, filename: str, content_type: str
    ) -> AnalysisResult: ...

    async def answer_question(
        self, text: str, question: str, *, filename: str
    ) -> AnswerResult: ...


def truncate_for_model(text: str, max_chars: int) -> tuple[str, bool]:
    """Clip text to a character budget, preferring a paragraph boundary.

    Returns ``(text, was_truncated)``. Cutting mid-sentence measurably degrades
    LLM output quality, so we back up to the last paragraph break within the
    final 20% of the budget when one exists.
    """
    if len(text) <= max_chars:
        return text, False

    window = text[:max_chars]
    boundary = window.rfind("\n\n")
    if boundary > int(max_chars * 0.8):
        return window[:boundary], True
    return window, True
