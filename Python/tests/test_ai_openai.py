"""Unit tests for the OpenAI adapter.

The real API needs a funded key, so the SDK client is replaced with a stub that
returns canned payloads. That is enough to cover what actually breaks in
production: the request we build, the schema we send, and - most importantly -
how we handle a response that is *shaped* correctly but semantically wrong.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from app.core.exceptions import AIProviderError, RateLimitedError
from app.schemas.document import DocumentAnalysis, DocumentKind, EntityType
from app.services.ai.openai_analyzer import (
    OpenAIAnalyzer,
    _analysis_schema,
    _answer_schema,
    _clamp_confidence,
    _coerce_analysis,
    _translate_error,
)


# ------------------------------------------------------------------ stub client
@dataclass
class _Message:
    content: str | None
    refusal: str | None = None


@dataclass
class _Choice:
    message: _Message
    finish_reason: str = "stop"


@dataclass
class _Usage:
    prompt_tokens: int = 1234
    completion_tokens: int = 321


class _Response:
    def __init__(self, content: str | None, *, finish_reason: str = "stop", refusal: str | None = None):
        self.choices = [_Choice(_Message(content, refusal), finish_reason)]
        self.usage = _Usage()


class _Completions:
    def __init__(self, response: Any, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response


class _Chat:
    def __init__(self, completions: _Completions) -> None:
        self.completions = completions


class FakeClient:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.completions = _Completions(response, error)
        self.chat = _Chat(self.completions)


def make_analyzer(response: Any = None, error: Exception | None = None) -> tuple[OpenAIAnalyzer, FakeClient]:
    analyzer = OpenAIAnalyzer(api_key="sk-test-not-real", model="gpt-4o-mini")
    fake = FakeClient(response, error)
    analyzer._client = fake  # bypass lazy construction
    return analyzer, fake


GOOD_PAYLOAD = {
    "document_type": "invoice",
    "language": "en",
    "summary": "An invoice from Acme to Northwind for hosting services.",
    "keywords": ["invoice", "hosting", "acme"],
    "entities": [
        {"text": "Acme Technologies Ltd", "type": "organization", "confidence": 0.95},
        {"text": "1488.00", "type": "money", "confidence": 0.9},
    ],
    "fields": [
        {"key": "invoice_number", "value": "INV-2024-00871", "confidence": 0.98},
        {"key": "total_amount", "value": "1488.00", "confidence": 0.97},
    ],
    "confidence": 0.93,
    "warnings": [],
}


# ---------------------------------------------------------------------- schema
def test_schema_matches_the_pydantic_model() -> None:
    """Guards against the hand-written provider schema drifting from the model.

    The JSON Schema is written by hand (strict mode rejects Pydantic's validation
    keywords), so this test is what keeps the two definitions in step.
    """
    schema = _analysis_schema()
    assert set(schema["properties"]) == set(DocumentAnalysis.model_fields)
    # Strict mode requires every property to be required and no extras allowed.
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False


def test_schema_enums_are_generated_from_the_shared_enums() -> None:
    schema = _analysis_schema()
    assert schema["properties"]["document_type"]["enum"] == [k.value for k in DocumentKind]
    entity_schema = schema["properties"]["entities"]["items"]
    assert entity_schema["properties"]["type"]["enum"] == [e.value for e in EntityType]
    assert entity_schema["additionalProperties"] is False
    assert set(entity_schema["required"]) == set(entity_schema["properties"])


def test_answer_schema_is_strict() -> None:
    schema = _answer_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"answer", "answer_found", "quotes"}


# --------------------------------------------------------------------- analyse
async def test_analyze_parses_a_good_response() -> None:
    analyzer, _ = make_analyzer(_Response(json.dumps(GOOD_PAYLOAD)))
    result = await analyzer.analyze(
        "Invoice text here", filename="invoice.pdf", content_type="application/pdf"
    )

    assert result.provider == "openai"
    assert result.model == "gpt-4o-mini"
    assert result.prompt_tokens == 1234
    assert result.completion_tokens == 321
    assert result.analysis.document_type is DocumentKind.INVOICE
    assert result.analysis.fields[0].key == "invoice_number"


async def test_analyze_requests_strict_structured_output() -> None:
    """Without strict mode we would be parsing free-form prose and hoping."""
    analyzer, fake = make_analyzer(_Response(json.dumps(GOOD_PAYLOAD)))
    await analyzer.analyze("text", filename="a.pdf", content_type="application/pdf")

    request = fake.completions.calls[0]
    response_format = request["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert request["model"] == "gpt-4o-mini"
    assert request["messages"][0]["role"] == "system"
    assert "Invent" not in request["messages"][0]["content"]  # sanity on the prompt
    assert "text" in request["messages"][1]["content"]


async def test_long_documents_are_truncated_with_a_warning() -> None:
    """A 300-page scan must not blow the context window or the budget silently."""
    from app.core.config import settings

    analyzer, fake = make_analyzer(_Response(json.dumps(GOOD_PAYLOAD)))
    huge = "word " * 40_000
    result = await analyzer.analyze(huge, filename="big.pdf", content_type="application/pdf")

    sent = fake.completions.calls[0]["messages"][1]["content"]
    assert len(sent) < len(huge)
    assert "truncated" in sent.lower()
    # The user is told their document was clipped, rather than quietly losing data.
    assert any("truncated" in warning.lower() for warning in result.analysis.warnings)
    assert str(settings.LLM_MAX_INPUT_CHARS) in " ".join(result.analysis.warnings)


# --------------------------------------------------------- malformed responses
async def test_malformed_json_becomes_a_domain_error() -> None:
    analyzer, _ = make_analyzer(_Response("this is not json at all"))
    with pytest.raises(AIProviderError, match="malformed JSON"):
        await analyzer.analyze("t", filename="a.pdf", content_type="application/pdf")


async def test_a_truncated_response_is_reported_clearly() -> None:
    """finish_reason='length' means cut-off JSON; a JSONDecodeError would mislead."""
    analyzer, _ = make_analyzer(_Response('{"document_type": "inv', finish_reason="length"))
    with pytest.raises(AIProviderError, match="cut off"):
        await analyzer.analyze("t", filename="a.pdf", content_type="application/pdf")


async def test_a_refusal_is_surfaced() -> None:
    analyzer, _ = make_analyzer(_Response(None, refusal="I cannot help with that."))
    with pytest.raises(AIProviderError, match="refused"):
        await analyzer.analyze("t", filename="a.pdf", content_type="application/pdf")


async def test_a_json_array_is_rejected() -> None:
    analyzer, _ = make_analyzer(_Response("[1, 2, 3]"))
    with pytest.raises(AIProviderError, match="not an object"):
        await analyzer.analyze("t", filename="a.pdf", content_type="application/pdf")


async def test_semantically_wrong_values_are_repaired_not_fatal() -> None:
    """Strict schemas constrain shape, not sanity - one bad value must not lose the doc."""
    analyzer, _ = make_analyzer(
        _Response(
            json.dumps(
                {
                    **GOOD_PAYLOAD,
                    "document_type": "purchase_order_thing",  # not in the enum
                    "confidence": 92,  # a percentage, not a fraction
                    "entities": [{"text": "X Ltd", "type": "corporation", "confidence": 5}],
                }
            )
        )
    )
    result = await analyzer.analyze("t", filename="a.pdf", content_type="application/pdf")

    assert result.analysis.document_type is DocumentKind.OTHER
    assert result.analysis.confidence == 0.92
    assert result.analysis.entities[0].type is EntityType.OTHER
    # Any value above 1 is read as a percentage - the same rule that turns the
    # document-level 92 into 0.92, applied consistently.
    assert result.analysis.entities[0].confidence == 0.05


# ------------------------------------------------------------------- coercion
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0.5, 0.5),
        (1, 1.0),
        (0, 0.0),
        (92, 0.92),  # percentage
        (100, 1.0),
        (150, 1.0),  # clamped
        (-3, 0.0),  # clamped
        ("0.7", 0.7),  # numeric string
        ("nonsense", None),
        (None, None),
        (float("nan"), None),
    ],
)
def test_confidence_is_coerced_and_clamped(raw: object, expected: float | None) -> None:
    assert _clamp_confidence(raw) == expected


def test_coercion_drops_unusable_entries_and_caps_sizes() -> None:
    coerced = _coerce_analysis(
        {
            "document_type": "invoice",
            "summary": "S" * 9000,
            "keywords": ["  spaced  ", "", 5, "ok"],
            "entities": ["not-a-dict", {"text": "   "}, {"text": "Real Co", "type": "organization"}],
            "fields": [{"key": "", "value": "x"}, {"key": "total", "value": 1488.0}],
            "confidence": 0.4,
            "warnings": ["", "  real warning "],
        }
    )
    assert len(coerced["summary"]) == 4000
    assert coerced["keywords"] == ["  spaced  ", "ok"]
    assert len(coerced["entities"]) == 1
    assert coerced["entities"][0]["text"] == "Real Co"
    # Non-string values are stringified rather than discarded.
    assert coerced["fields"] == [{"key": "total", "value": "1488.0", "confidence": None}]
    assert coerced["warnings"] == ["real warning"]

    # The repaired payload must validate against the model.
    assert DocumentAnalysis.model_validate(coerced)


def test_missing_keys_fall_back_to_safe_defaults() -> None:
    coerced = _coerce_analysis({})
    assert coerced["document_type"] == DocumentKind.OTHER.value
    assert coerced["confidence"] == 0.0
    assert DocumentAnalysis.model_validate(coerced)


# ---------------------------------------------------------------------- answer
async def test_answer_question_returns_grounded_quotes() -> None:
    analyzer, _ = make_analyzer(
        _Response(
            json.dumps(
                {
                    "answer": "The total due is 1488.00.",
                    "answer_found": True,
                    "quotes": ["Total Amount: 1488.00", "  ", "Amount Due: 1488.00"],
                }
            )
        )
    )
    result = await analyzer.answer_question("doc text", "What is due?", filename="a.pdf")

    assert result.answer_found is True
    assert result.quotes == ["Total Amount: 1488.00", "Amount Due: 1488.00"]  # blanks dropped
    assert result.provider == "openai"


async def test_answer_found_is_false_when_the_answer_is_empty() -> None:
    """A blank answer must never be reported as a successful find."""
    analyzer, _ = make_analyzer(
        _Response(json.dumps({"answer": "   ", "answer_found": True, "quotes": []}))
    )
    result = await analyzer.answer_question("doc", "q?", filename="a.pdf")
    assert result.answer_found is False


# ------------------------------------------------------------ error translation
def test_sdk_errors_map_to_actionable_domain_errors() -> None:
    import httpx
    import openai

    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")

    def response(code: int) -> httpx.Response:
        return httpx.Response(code, request=request)

    auth = _translate_error(
        openai.AuthenticationError("bad key", response=response(401), body=None)
    )
    assert isinstance(auth, AIProviderError)
    assert "OPENAI_API_KEY" in auth.message

    limited = _translate_error(
        openai.RateLimitError("slow down", response=response(429), body=None)
    )
    assert isinstance(limited, RateLimitedError)

    timeout = _translate_error(openai.APITimeoutError(request=request))
    assert isinstance(timeout, AIProviderError)
    assert "respond" in timeout.message

    connection = _translate_error(openai.APIConnectionError(request=request))
    assert connection.status_code == 503

    unknown = _translate_error(ValueError("something odd"))
    assert isinstance(unknown, AIProviderError)


async def test_a_provider_error_propagates_from_analyze() -> None:
    import httpx
    import openai

    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    analyzer, _ = make_analyzer(
        error=openai.AuthenticationError(
            "nope", response=httpx.Response(401, request=request), body=None
        )
    )
    with pytest.raises(AIProviderError):
        await analyzer.analyze("t", filename="a.pdf", content_type="application/pdf")
