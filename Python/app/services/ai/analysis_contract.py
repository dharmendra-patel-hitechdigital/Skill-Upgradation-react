"""The provider-independent half of document analysis.

Every LLM-backed analyser needs the same four things: the system prompts, the
JSON Schema the model must fill in, the user-message builders, and the liberal
coercion pass that turns a *shape-valid* model response into something
``DocumentAnalysis`` will accept. None of that is provider-specific.

It lives here rather than in one adapter because the alternative is two copies.
A schema and a coercion table duplicated per provider drift silently: someone
adds a document type, updates one file, and the other provider quietly keeps
classifying that type as ``other`` - with no test failure, because each adapter
tests its own copy.

What stays in each adapter is exactly what differs: the SDK call, and the
translation of that SDK's exceptions into domain errors.
"""
from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.schemas.document import DocumentKind, EntityType

MAX_SUMMARY_CHARS = 4000
MAX_QUOTES = 5

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


def analysis_schema() -> dict[str, Any]:
    """JSON Schema for a structured-output analysis request.

    Written by hand rather than derived from
    ``DocumentAnalysis.model_json_schema()`` because strict structured-output
    modes reject the validation keywords Pydantic emits (``maxLength``,
    ``exclusiveMinimum``, ``$defs`` defaults). The *enums* are still generated
    from the shared StrEnums, and a unit test asserts the property set matches
    the Pydantic model - so this cannot silently drift.

    The shape satisfies both providers' strict modes: every property is listed
    in ``required`` and every object sets ``additionalProperties: false``.
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


def answer_schema() -> dict[str, Any]:
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


def build_analysis_prompt(text: str, *, filename: str, content_type: str, truncated: bool) -> str:
    """The user message for an analysis request, identical across providers."""
    truncation_note = (
        "NOTE: the text below was truncated to fit the context window.\n"
        if truncated
        else ""
    )
    return (
        f"Filename: {filename}\n"
        f"Content type: {content_type}\n"
        f"{truncation_note}"
        f"\n--- DOCUMENT TEXT START ---\n{text}\n--- DOCUMENT TEXT END ---"
    )


def build_answer_prompt(text: str, question: str, *, filename: str, truncated: bool) -> str:
    return (
        f"Question: {question}\n\n"
        f"Document: {filename}"
        f"{' (text truncated)' if truncated else ''}\n"
        f"\n--- DOCUMENT TEXT START ---\n{text}\n--- DOCUMENT TEXT END ---"
    )


def truncation_warning() -> str:
    return (
        "The document was truncated to the first "
        f"{settings.LLM_MAX_INPUT_CHARS} characters for analysis; content beyond "
        "that point was not considered."
    )


def clamp_confidence(value: Any) -> float | None:
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


def coerce_analysis(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalise a raw model response into something ``DocumentAnalysis`` accepts.

    Strict schemas constrain shape, not semantics: a model can still return a
    confidence of ``1.4``, a 30 000-character summary, or an entity type it
    invented despite the enum. Repairing those here keeps one bad value from
    discarding an otherwise good analysis.

    This is also why the adapters do not use their SDKs' "parse straight into a
    Pydantic model" helpers - those validate strictly, so a single out-of-range
    number would fail the whole document instead of degrading one field.
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
                "type": (
                    entity_type if entity_type in valid_entities else EntityType.OTHER.value
                ),
                "confidence": clamp_confidence(raw.get("confidence")),
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
                "confidence": clamp_confidence(raw.get("confidence")),
            }
        )

    language = payload.get("language")
    warnings = [
        str(item).strip() for item in (payload.get("warnings") or []) if str(item).strip()
    ]

    return {
        "document_type": kind if kind in valid_kinds else DocumentKind.OTHER.value,
        "language": str(language)[:16] if language else None,
        "summary": summary[:MAX_SUMMARY_CHARS],
        # Strings only: a stray number or object in this list is noise, not a
        # keyword, and stringifying it would surface "5" as a topic.
        "keywords": [
            k for k in (payload.get("keywords") or []) if isinstance(k, str) and k.strip()
        ][:25],
        "entities": entities[:100],
        "fields": fields[:100],
        "confidence": clamp_confidence(payload.get("confidence")) or 0.0,
        "warnings": warnings[:20],
    }


def coerce_answer(payload: dict[str, Any]) -> tuple[str, bool, list[str]]:
    """Normalise an answer response into ``(answer, answer_found, quotes)``.

    ``answer_found`` is ANDed with a non-empty answer: a model that sets the flag
    but returns nothing has not found anything, and reporting otherwise would
    show the user a confident blank.
    """
    answer = str(payload.get("answer") or "").strip()
    quotes = [
        str(quote).strip()
        for quote in (payload.get("quotes") or [])
        if str(quote).strip()
    ][:MAX_QUOTES]
    return answer, bool(payload.get("answer_found")) and bool(answer), quotes
