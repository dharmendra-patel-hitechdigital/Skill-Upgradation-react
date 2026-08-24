"""Provider selection.

One place decides which engines run, based on configuration and on what is
actually reachable. The rest of the application asks for "a text extractor for
this content type" and gets one.

The ``auto`` policy is what makes the service degrade gracefully instead of
failing: use the best configured provider, fall back to the built-in one, and
report honestly which was chosen so the ``/health/providers`` endpoint and every
stored extraction record show real provenance.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache

from app.core.config import settings
from app.core.exceptions import AIProviderUnavailableError, ExtractionError
from app.services.ai.base import DocumentAnalyzer, TextExtractor
from app.services.ai.claude_analyzer import ClaudeAnalyzer
from app.services.ai.heuristic import HeuristicAnalyzer
from app.services.ai.local_text import LocalTextExtractor
from app.services.ai.openai_analyzer import OpenAIAnalyzer
from app.services.ai.textract import TextractExtractor

logger = logging.getLogger(__name__)

# Every value ``LLM_PROVIDER`` (or its runtime override) may take. Exported so the
# admin endpoint validates against one list rather than repeating it.
ANALYSIS_POLICIES: tuple[str, ...] = ("auto", "claude", "openai", "heuristic", "none")


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    """Human-readable view of the AI configuration, surfaced over HTTP."""

    text_extraction: str
    analysis: str
    textract_available: bool
    openai_available: bool
    anthropic_available: bool
    notes: list[str]


@dataclass(frozen=True, slots=True)
class AnalysisOption:
    """One selectable analysis engine, as offered to the admin panel."""

    id: str
    label: str
    description: str
    available: bool
    # Why it cannot be selected, or None when it can. Shown next to a disabled
    # option so an admin is never left guessing which secret is missing.
    unavailable_reason: str | None = None
    model: str | None = None


def _textract_enabled() -> bool:
    """Textract needs to be switched on *and* have credentials available.

    boto3 can also resolve credentials from an instance role, which we cannot
    detect cheaply here - so an explicit ``TEXTRACT_ENABLED`` flag is the switch,
    and static keys are merely one way to satisfy it.
    """
    return settings.TEXTRACT_ENABLED or settings.OCR_PROVIDER == "textract"


def _openai_enabled() -> bool:
    return bool(settings.OPENAI_API_KEY and settings.OPENAI_API_KEY.strip())


def _anthropic_enabled() -> bool:
    return bool(settings.ANTHROPIC_API_KEY and settings.ANTHROPIC_API_KEY.strip())


@lru_cache(maxsize=1)
def _local_extractor() -> LocalTextExtractor:
    return LocalTextExtractor()


@lru_cache(maxsize=1)
def _textract_extractor() -> TextractExtractor:
    # The async (multi-page) Textract API reads only from S3, so it is available
    # only when a bucket is configured.
    return TextractExtractor(
        region=settings.AWS_REGION,
        staging_bucket=settings.S3_BUCKET,
    )


@lru_cache(maxsize=1)
def _heuristic_analyzer() -> HeuristicAnalyzer:
    return HeuristicAnalyzer()


@lru_cache(maxsize=1)
def _openai_analyzer() -> OpenAIAnalyzer:
    assert settings.OPENAI_API_KEY  # guarded by _openai_enabled()
    return OpenAIAnalyzer(
        api_key=settings.OPENAI_API_KEY,
        model=settings.OPENAI_MODEL,
        base_url=settings.OPENAI_BASE_URL,
    )


@lru_cache(maxsize=1)
def _claude_analyzer() -> ClaudeAnalyzer:
    assert settings.ANTHROPIC_API_KEY  # guarded by _anthropic_enabled()
    return ClaudeAnalyzer(
        api_key=settings.ANTHROPIC_API_KEY,
        model=settings.ANTHROPIC_MODEL,
        base_url=settings.ANTHROPIC_BASE_URL,
    )


def get_text_extractor(content_type: str) -> TextExtractor:
    """Pick the stage-1 engine for a content type.

    Selection order under ``auto``: Textract if it is configured *and* handles
    this type, otherwise the local reader. A scanned image with no Textract
    configured raises immediately with an actionable message rather than storing
    an empty extraction and calling it success.
    """
    policy = settings.OCR_PROVIDER

    if policy == "none":
        raise AIProviderUnavailableError("Text extraction is disabled (OCR_PROVIDER=none).")

    if policy == "local":
        extractor = _local_extractor()
        if not extractor.supports(content_type):
            raise ExtractionError(
                f"The local text engine cannot read '{content_type}'. Set "
                "TEXTRACT_ENABLED=true to process scanned images.",
                details={"content_type": content_type},
            )
        return extractor

    if policy == "textract":
        if not _textract_enabled():  # pragma: no cover - config guard
            raise AIProviderUnavailableError(
                "OCR_PROVIDER=textract but Textract is not enabled."
            )
        return _textract_extractor()

    # policy == "auto"
    local = _local_extractor()
    if _textract_enabled():
        textract = _textract_extractor()
        # Prefer the local reader for PDFs and plain text: a digital PDF already
        # contains its characters, so paying Textract to re-read them is pure
        # cost and latency for no accuracy gain.
        if local.supports(content_type):
            return local
        if textract.supports(content_type):
            return textract
    elif local.supports(content_type):
        return local

    raise ExtractionError(
        f"No text-extraction engine is available for '{content_type}'. PDFs and "
        "plain text work out of the box; scanned images and photos require AWS "
        "Textract (set TEXTRACT_ENABLED=true).",
        details={"content_type": content_type},
    )


def effective_analysis_policy(override: str | None = None) -> str:
    """Resolve which policy applies: the runtime override, else the env default.

    An unrecognised override is ignored rather than raising, so a stale row in
    ``app_settings`` (a provider removed from a later build, say) degrades to the
    configured default instead of failing every upload.
    """
    if override and override in ANALYSIS_POLICIES:
        return override
    if override:
        logger.warning("unknown_analysis_policy_ignored", extra={"policy": override})
    return settings.LLM_PROVIDER


def get_analyzer(policy: str | None = None) -> DocumentAnalyzer:
    """Pick the stage-2 engine.

    ``policy`` is the runtime override chosen by an administrator; omit it to use
    the configured default. Selection is explicit rather than read from a
    process-local cache, so a change made on one replica applies to the next
    document processed by every replica.
    """
    policy = effective_analysis_policy(policy)

    if policy == "none":
        raise AIProviderUnavailableError("Document analysis is disabled (LLM_PROVIDER=none).")
    if policy == "heuristic":
        return _heuristic_analyzer()
    if policy == "claude":
        if not _anthropic_enabled():
            raise AIProviderUnavailableError(
                "The Claude analyser is selected but ANTHROPIC_API_KEY is not set."
            )
        return _claude_analyzer()
    if policy == "openai":
        if not _openai_enabled():
            raise AIProviderUnavailableError(
                "The OpenAI analyser is selected but OPENAI_API_KEY is not set."
            )
        return _openai_analyzer()

    # policy == "auto": the most capable configured engine, else the rule engine.
    # Claude first because it is the newer integration and the one whose model
    # default is a current frontier model; an installation that wants the other
    # order should say so explicitly rather than rely on this tie-break.
    if _anthropic_enabled():
        return _claude_analyzer()
    if _openai_enabled():
        return _openai_analyzer()
    return _heuristic_analyzer()


def get_analyzer_with_fallback(
    policy: str | None = None,
) -> tuple[DocumentAnalyzer, DocumentAnalyzer | None]:
    """Return ``(primary, fallback)`` for the analysis stage.

    Whenever the primary is a paid LLM, the rule-based analyser is offered as a
    fallback. That converts an outage or an exhausted quota from "every upload
    fails" into "documents still get processed, with a warning attached".

    Previously this applied only under ``auto``; it now applies to an explicit
    choice too. Choosing Claude or OpenAI in the admin panel is a statement about
    which engine to *prefer*, not a request to fail the document when that engine
    is down.
    """
    resolved = effective_analysis_policy(policy)
    primary = get_analyzer(resolved)
    if primary.name in ("claude", "openai"):
        return primary, _heuristic_analyzer()
    return primary, None


def analysis_options() -> list[AnalysisOption]:
    """Every selectable analysis engine, with whether it can actually run.

    Drives the admin panel's provider picker. Availability is derived from the
    configured secrets, so an option is never offered that would fail on the next
    upload - and a disabled one carries the reason.
    """
    anthropic_ready = _anthropic_enabled()
    openai_ready = _openai_enabled()

    return [
        AnalysisOption(
            id="auto",
            label="Automatic",
            description=(
                "Use the best configured engine: Claude, then OpenAI, then the "
                "built-in rule engine."
            ),
            available=True,
            model=None,
        ),
        AnalysisOption(
            id="claude",
            label="Claude (Anthropic)",
            description=(
                "Strongest results on dense documents - contracts, statements, "
                "poor scans."
            ),
            available=anthropic_ready,
            unavailable_reason=(
                None if anthropic_ready else "ANTHROPIC_API_KEY is not configured."
            ),
            model=settings.ANTHROPIC_MODEL,
        ),
        AnalysisOption(
            id="openai",
            label="OpenAI",
            description="Structured extraction via OpenAI's models.",
            available=openai_ready,
            unavailable_reason=(
                None if openai_ready else "OPENAI_API_KEY is not configured."
            ),
            model=settings.OPENAI_MODEL,
        ),
        AnalysisOption(
            id="heuristic",
            label="Built-in rule engine",
            description=(
                "No third-party calls, no cost, no network. Weaker summaries and "
                "fewer fields."
            ),
            available=True,
            model="rules-v1",
        ),
        AnalysisOption(
            id="none",
            label="Disabled",
            description=(
                "Reject analysis entirely. Uploads still store text but produce "
                "no extraction."
            ),
            available=True,
            model=None,
        ),
    ]


def describe_providers(policy: str | None = None) -> ProviderStatus:
    """Report the effective configuration for the health endpoint."""
    notes: list[str] = []

    textract_ready = _textract_enabled()
    openai_ready = _openai_enabled()
    anthropic_ready = _anthropic_enabled()

    if not textract_ready:
        notes.append(
            "AWS Textract is off: scanned images and photos cannot be processed. "
            "PDFs with a text layer and plain text work."
        )
    if not openai_ready and not anthropic_ready:
        notes.append(
            "No Anthropic or OpenAI key configured: the built-in rule-based "
            "analyser is in use."
        )
    elif not anthropic_ready:
        notes.append("No Anthropic key configured, so the Claude analyser cannot be used.")
    elif not openai_ready:
        notes.append("No OpenAI key configured, so the OpenAI analyser cannot be used.")
    if textract_ready and not settings.S3_BUCKET:
        notes.append(
            "Textract is on but no S3 bucket is set, so PDFs larger than "
            f"{settings.TEXTRACT_MAX_SYNC_BYTES // (1024 * 1024)} MB cannot be processed."
        )

    try:
        analysis = get_analyzer(policy).name
    except AIProviderUnavailableError:
        analysis = "disabled"

    if settings.OCR_PROVIDER == "none":
        extraction = "disabled"
    elif settings.OCR_PROVIDER == "auto":
        extraction = "local+textract" if textract_ready else "local"
    else:
        extraction = settings.OCR_PROVIDER

    return ProviderStatus(
        text_extraction=extraction,
        analysis=analysis,
        textract_available=textract_ready,
        openai_available=openai_ready,
        anthropic_available=anthropic_ready,
        notes=notes,
    )


def reset_provider_cache() -> None:
    """Clear memoised providers - used by tests that flip configuration."""
    for factory in (
        _local_extractor,
        _textract_extractor,
        _heuristic_analyzer,
        _openai_analyzer,
        _claude_analyzer,
    ):
        factory.cache_clear()
