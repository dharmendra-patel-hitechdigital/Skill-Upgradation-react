"""OpenAI adapter for document understanding and grounded question answering.

Three decisions in here are what separate a reliable feature from a demo:

**1. Structured outputs, not prompt-and-pray.** The request carries a JSON Schema
with ``strict: true``, so the model is constrained at decode time to emit exactly
our keys, types, and enum members. We never regex a fenced code block out of
prose, and "the model replied in a slightly different shape today" stops being a
failure mode.

**2. The schema is generated from the same enums the API serves.** Adding a
document type in one place updates the model's allowed values, the database
value, and the OpenAPI docs together, so they cannot drift apart.

**3. Liberal parsing of a constrained response.** Even with strict schemas, an
LLM can return a confidence of ``1.4`` or a 30 000-character summary. Values are
coerced and clamped *before* Pydantic validation, so one out-of-range number
degrades a field rather than failing the whole document.

Grounding: the prompts forbid outside knowledge, and the answer schema carries an
explicit ``answer_found`` flag plus verbatim ``quotes``. A model that cannot find
the answer is given a way to say so, which is the cheapest hallucination defence
available.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from app.core.config import settings
from app.core.exceptions import AIProviderError
from app.schemas.document import (
    DocumentAnalysis,
    DocumentKind,
    EntityType,
)
from app.services.ai.base import (
    AnalysisResult,
    AnswerResult,
    truncate_for_model,
)

logger = logging.getLogger(__name__)

_MAX_SUMMARY_CHARS = 4000
_MAX_QUOTES = 5

ANALYSIS_SYSTEM_PROMPT = """\
You are a precise document-analysis engine. You are given the raw text of a \
single document, extracted by OCR or from a PDF text layer.

Rules:
- Use ONLY the provided text. Never infer, complete, or invent facts that are \
not present.
- Copy values EXACTLY as they appear (amounts, dates, identifiers, names). Do \
not reformat, convert currencies, or normalise dates.
- If a value is genuinely absent, omit that field rather than guessing.
- `summary` must be 2-4 sentences describing what the document is and its key \
content. No preamble such as "This document is...".
- `fields` must hold the document's business-critical key/value pairs, using \
lower_snake_case keys (invoice_number, total_amount, due_date, vendor_name, \
account_number). Prefer 5-15 of the most important pairs.
- `confidence` reflects how legible and complete the text was, not how \
confident you feel in general. Text that is clearly truncated or garbled should \
score below 0.5.
- Add a short note to `warnings` for anything that limited the analysis \
(truncated input, unreadable pages, mixed languages).
- OCR text may contain layout noise and `[page N]` markers. Ignore those \
markers as content.\
"""

ANSWER_SYSTEM_PROMPT = """\
You answer questions about ONE document, using only its text.

Rules:
- If the answer is not present in the text, set `answer_found` to false and \
explain briefly in `answer` what is missing. Never guess.
- When the answer IS present, set `answer_found` to true and quote the exact \
supporting snippets in `quotes` (verbatim, short - one or two lines each).
- Answer in the same language as the question.
- Be direct and specific. State the value, then the context if needed.\
"""


def _analysis_schema() -> dict[str, Any]:
    """JSON Schema handed to OpenAI's strict structured-output mode.

    Written by hand rather than derived from ``DocumentAnalysis.model_json_schema()``
    because strict mode rejects the validation keywords Pydantic emits
    (``maxLength``, ``exclusiveMinimum``, ``$defs`` defaults). The *enums* are
    still generated from the shared StrEnums, and a unit test asserts the
    property set matches the Pydantic model - so this cannot silently drift.
    """
    confidence = {
        "type": ["number", "null"],
        "description": "Confidence between 0 and 1.",
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "document_type",
            "language",
            "summary",
            "keywords",
            "entities",
            "fields",
            "confidence",
            "warnings",
        ],
        "properties": {
            "document_type": {
                "type": "string",
                "enum": [kind.value for kind in DocumentKind],
                "description": "Best-fit classification of the document.",
            },
            "language": {
                "type": ["string", "null"],
                "description": "ISO 639-1 code of the dominant language, e.g. 'en'.",
            },
            "summary": {"type": "string", "description": "Two to four sentences."},
            "keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Up to 15 salient terms or topics.",
            },
            "entities": {
                "type": "array",
                "description": "Named things mentioned in the document.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["text", "type", "confidence"],
                    "properties": {
                        "text": {"type": "string"},
                        "type": {
                            "type": "string",
                            "enum": [entity.value for entity in EntityType],
                        },
                        "confidence": confidence,
                    },
                },
            },
            "fields": {
                "type": "array",
                "description": "Business-critical key/value pairs.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["key", "value", "confidence"],
                    "properties": {
                        "key": {
                            "type": "string",
                            "description": "lower_snake_case field name.",
                        },
                        "value": {"type": ["string", "null"]},
                        "confidence": confidence,
                    },
                },
            },
            "confidence": {
                "type": "number",
                "description": "Overall confidence in this analysis, 0 to 1.",
            },
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
    }


def _answer_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["answer", "answer_found", "quotes"],
        "properties": {
            "answer": {"type": "string"},
            "answer_found": {
                "type": "boolean",
                "description": "False when the document does not contain the answer.",
            },
            "quotes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Verbatim supporting snippets from the document.",
            },
        },
    }


class OpenAIAnalyzer:
    """Document analysis and Q&A backed by an OpenAI chat model."""

    name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> None:
        self._api_key = api_key
        self.model = model or settings.OPENAI_MODEL
        self._base_url = base_url or settings.OPENAI_BASE_URL
        self._timeout = timeout if timeout is not None else settings.OPENAI_TIMEOUT_SECONDS
        self._max_retries = (
            max_retries if max_retries is not None else settings.OPENAI_MAX_RETRIES
        )
        self._client: Any = None

    def _get_client(self) -> Any:
        """Build the client once. It owns a connection pool worth reusing."""
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:  # pragma: no cover
                raise AIProviderError("The 'openai' package is not installed.") from exc

            kwargs: dict[str, Any] = {
                "api_key": self._api_key,
                "timeout": self._timeout,
                # The SDK retries connection errors, 429s and 5xx with jittered
                # backoff. Letting it do that is strictly better than a hand-rolled
                # loop, which would also retry deterministic 400s.
                "max_retries": self._max_retries,
            }
            if self._base_url:
                kwargs["base_url"] = self._base_url
            self._client = AsyncOpenAI(**kwargs)
        return self._client

    # ------------------------------------------------------------------ analyse
    async def analyze(
        self, text: str, *, filename: str, content_type: str
    ) -> AnalysisResult:
        prompt_text, truncated = truncate_for_model(text, settings.LLM_MAX_INPUT_CHARS)

        truncation_note = (
            "NOTE: the text below was truncated to fit the context window.\n"
            if truncated
            else ""
        )
        user_content = (
            f"Filename: {filename}\n"
            f"Content type: {content_type}\n"
            f"{truncation_note}"
            f"\n--- DOCUMENT TEXT START ---\n{prompt_text}\n--- DOCUMENT TEXT END ---"
        )

        started = time.perf_counter()
        payload, usage = await self._complete(
            system=ANALYSIS_SYSTEM_PROMPT,
            user=user_content,
            schema_name="document_analysis",
            schema=_analysis_schema(),
        )
        duration_ms = int((time.perf_counter() - started) * 1000)

        coerced = _coerce_analysis(payload)
        if truncated:
            coerced.setdefault("warnings", []).append(
                "The document was truncated to the first "
                f"{settings.LLM_MAX_INPUT_CHARS} characters for analysis; content "
                "beyond that point was not considered."
            )

        try:
            analysis = DocumentAnalysis.model_validate(coerced)
        except Exception as exc:
            raise AIProviderError(
                "The AI provider returned an analysis that failed validation.",
                details={"reason": str(exc)[:500]},
            ) from exc

        return AnalysisResult(
            analysis=analysis,
            provider=self.name,
            model=self.model,
            duration_ms=duration_ms,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )

    # ------------------------------------------------------------------- answer
    async def answer_question(
        self, text: str, question: str, *, filename: str
    ) -> AnswerResult:
        prompt_text, truncated = truncate_for_model(text, settings.LLM_MAX_INPUT_CHARS)
        user_content = (
            f"Question: {question}\n\n"
            f"Document: {filename}"
            f"{' (text truncated)' if truncated else ''}\n"
            f"\n--- DOCUMENT TEXT START ---\n{prompt_text}\n--- DOCUMENT TEXT END ---"
        )

        payload, _ = await self._complete(
            system=ANSWER_SYSTEM_PROMPT,
            user=user_content,
            schema_name="document_answer",
            schema=_answer_schema(),
        )

        answer = str(payload.get("answer") or "").strip()
        quotes = [
            str(quote).strip()
            for quote in (payload.get("quotes") or [])
            if str(quote).strip()
        ][:_MAX_QUOTES]

        return AnswerResult(
            answer=answer or "The model returned an empty answer.",
            answer_found=bool(payload.get("answer_found")) and bool(answer),
            quotes=quotes,
            provider=self.name,
            model=self.model,
        )

    # ------------------------------------------------------------------- shared
    async def _complete(
        self, *, system: str, user: str, schema_name: str, schema: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, int | None]]:
        """One structured-output chat completion, with errors mapped to domain errors."""
        client = self._get_client()

        request: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": schema, "strict": True},
            },
        }
        # Reasoning models reject any temperature other than the default, so this
        # is opt-out via `OPENAI_TEMPERATURE=` (empty) in the environment.
        if settings.OPENAI_TEMPERATURE is not None:
            request["temperature"] = settings.OPENAI_TEMPERATURE

        try:
            response = await client.chat.completions.create(**request)
        except Exception as exc:
            raise _translate_error(exc) from exc

        choice = response.choices[0] if response.choices else None
        if choice is None:
            raise AIProviderError("The AI provider returned no completion.")

        # A length-capped response is truncated JSON; failing here with a clear
        # message beats a confusing JSONDecodeError.
        if choice.finish_reason == "length":
            raise AIProviderError(
                "The AI response was cut off before it was complete. Try a smaller "
                "document or raise the model's output limit."
            )
        if getattr(choice.message, "refusal", None):
            raise AIProviderError(
                "The AI provider refused to process this document.",
                details={"refusal": str(choice.message.refusal)[:300]},
            )

        content = choice.message.content or ""
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise AIProviderError(
                "The AI provider returned malformed JSON.",
                details={"snippet": content[:200]},
            ) from exc

        if not isinstance(payload, dict):
            raise AIProviderError("The AI provider returned JSON that was not an object.")

        usage_obj = getattr(response, "usage", None)
        usage = {
            "prompt_tokens": getattr(usage_obj, "prompt_tokens", None),
            "completion_tokens": getattr(usage_obj, "completion_tokens", None),
        }
        return payload, usage


# ------------------------------------------------------------------- coercion
def _clamp_confidence(value: Any) -> float | None:
    """Coerce anything to a 0-1 float, or None. Never raises."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    # Models sometimes emit percentages (92) instead of fractions (0.92).
    if 1.0 < number <= 100.0:
        number /= 100.0
    return max(0.0, min(1.0, number))


def _coerce_analysis(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalise a raw model response into something ``DocumentAnalysis`` accepts.

    Strict schemas constrain shape, not semantics: the model can still return an
    out-of-range confidence, an over-long summary, or an entity type it invented
    despite the enum. Repairing those here keeps one bad value from discarding an
    otherwise good analysis.
    """
    valid_kinds = {kind.value for kind in DocumentKind}
    valid_entities = {entity.value for entity in EntityType}

    kind = str(payload.get("document_type") or "").strip().lower()
    summary = str(payload.get("summary") or "").strip()

    entities: list[dict[str, Any]] = []
    for raw in payload.get("entities") or []:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or "").strip()
        if not text:
            continue
        entity_type = str(raw.get("type") or "").strip().lower()
        entities.append(
            {
                "text": text[:512],
                "type": entity_type if entity_type in valid_entities else EntityType.OTHER.value,
                "confidence": _clamp_confidence(raw.get("confidence")),
            }
        )

    fields: list[dict[str, Any]] = []
    for raw in payload.get("fields") or []:
        if not isinstance(raw, dict):
            continue
        key = str(raw.get("key") or "").strip()
        if not key:
            continue
        value = raw.get("value")
        fields.append(
            {
                "key": key[:128],
                "value": None if value is None else str(value)[:2048],
                "confidence": _clamp_confidence(raw.get("confidence")),
            }
        )

    language = payload.get("language")
    warnings = [
        str(item).strip()
        for item in (payload.get("warnings") or [])
        if str(item).strip()
    ]

    return {
        "document_type": kind if kind in valid_kinds else DocumentKind.OTHER.value,
        "language": str(language)[:16] if language else None,
        "summary": summary[:_MAX_SUMMARY_CHARS],
        # Strings only: a stray number or object in this list is noise, not a
        # keyword, and stringifying it would surface "5" as a topic.
        "keywords": [
            k for k in (payload.get("keywords") or []) if isinstance(k, str) and k.strip()
        ][:25],
        "entities": entities[:100],
        "fields": fields[:100],
        "confidence": _clamp_confidence(payload.get("confidence")) or 0.0,
        "warnings": warnings[:20],
    }


def _translate_error(exc: Exception) -> Exception:
    """Map OpenAI SDK exceptions onto domain errors with operator-useful text."""
    try:
        import openai
    except ImportError:  # pragma: no cover
        return AIProviderError(f"OpenAI call failed: {exc}")

    from app.core.exceptions import AIProviderUnavailableError, RateLimitedError

    if isinstance(exc, openai.AuthenticationError):
        return AIProviderError(
            "OpenAI rejected the API key. Check OPENAI_API_KEY.", code="ai_auth_failed"
        )
    if isinstance(exc, openai.PermissionDeniedError):
        return AIProviderError(
            "This OpenAI account is not permitted to use the configured model.",
            details={"model": settings.OPENAI_MODEL},
        )
    if isinstance(exc, openai.NotFoundError):
        return AIProviderError(
            f"The model '{settings.OPENAI_MODEL}' does not exist or is unavailable "
            "to this account.",
        )
    if isinstance(exc, openai.RateLimitError):
        # Distinguished from a transient 429 by the SDK having already retried.
        return RateLimitedError(
            "The OpenAI rate limit or quota was exhausted. Retry this document later."
        )
    if isinstance(exc, openai.APITimeoutError):
        return AIProviderError(
            f"OpenAI did not respond within {settings.OPENAI_TIMEOUT_SECONDS:.0f}s."
        )
    if isinstance(exc, openai.APIConnectionError):
        return AIProviderUnavailableError("Could not reach the OpenAI API.")
    if isinstance(exc, openai.BadRequestError):
        return AIProviderError(
            "OpenAI rejected the request as invalid.", details={"reason": str(exc)[:300]}
        )
    if isinstance(exc, openai.APIStatusError):
        return AIProviderError(
            f"OpenAI returned an error (HTTP {exc.status_code}).",
            details={"reason": str(exc)[:300]},
        )
    return AIProviderError(f"Unexpected OpenAI failure: {exc}")
