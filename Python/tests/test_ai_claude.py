"""The Claude analyser: request shape, response handling, and error mapping.

No network. The SDK client is replaced with a stub that records the request and
returns a canned response, so these tests assert on the contract this adapter has
with the Anthropic API - which is what actually breaks when the API moves.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from app.core.exceptions import (
    AIProviderError,
    AIProviderUnavailableError,
    RateLimitedError,
)
from app.services.ai.claude_analyzer import ClaudeAnalyzer, _first_text_block, _translate_error

anthropic = pytest.importorskip("anthropic")


# --------------------------------------------------------------------- doubles
class _Block:
    def __init__(self, type_: str, text: str | None = None) -> None:
        self.type = type_
        self.text = text


class _Usage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _Response:
    def __init__(
        self,
        *,
        content: list[_Block],
        stop_reason: str = "end_turn",
        usage: _Usage | None = None,
        stop_details: Any = None,
    ) -> None:
        self.content = content
        self.stop_reason = stop_reason
        self.usage = usage or _Usage(1200, 300)
        self.stop_details = stop_details


class _Messages:
    def __init__(self, response: Any) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class _Client:
    def __init__(self, response: Any) -> None:
        self.messages = _Messages(response)


def _analyzer(response: Any) -> tuple[ClaudeAnalyzer, _Client]:
    analyzer = ClaudeAnalyzer(api_key="test-key", model="claude-opus-5")
    client = _Client(response)
    analyzer._client = client  # bypass the lazy SDK construction
    return analyzer, client


ANALYSIS_JSON = {
    "document_type": "invoice",
    "language": "en",
    "summary": "An invoice from Acme for consulting work totalling $1,240.50.",
    "keywords": ["invoice", "acme"],
    "entities": [{"text": "Acme", "type": "organization", "confidence": 0.9}],
    "fields": [{"key": "invoice_total", "value": "1240.50", "confidence": 0.95}],
    "confidence": 0.91,
    "warnings": [],
}


# ------------------------------------------------------------- request contract
async def test_analyze_sends_the_documented_request_shape() -> None:
    """Guards the four things the Anthropic API is strict about."""
    analyzer, client = _analyzer(
        _Response(content=[_Block("text", json.dumps(ANALYSIS_JSON))])
    )

    await analyzer.analyze("Invoice text", filename="inv.pdf", content_type="application/pdf")

    request = client.messages.calls[0]
    assert request["model"] == "claude-opus-5"
    # The system prompt is a top-level parameter, not a role="system" message.
    assert isinstance(request["system"], str)
    assert [message["role"] for message in request["messages"]] == ["user"]
    # Structured output: effort and format both live inside output_config.
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert request["output_config"]["effort"] in ("low", "medium", "high", "xhigh", "max")
    # Adaptive thinking, never a fixed token budget - budget_tokens is rejected
    # outright on current models.
    assert request["thinking"] == {"type": "adaptive"}
    assert "budget_tokens" not in json.dumps(request)
    assert request["max_tokens"] > 0


async def test_analyze_maps_usage_onto_the_neutral_token_names() -> None:
    """Anthropic reports input/output; the extraction record stores prompt/completion."""
    analyzer, _ = _analyzer(
        _Response(
            content=[_Block("text", json.dumps(ANALYSIS_JSON))],
            usage=_Usage(input_tokens=4321, output_tokens=765),
        )
    )

    result = await analyzer.analyze("text", filename="a.pdf", content_type="application/pdf")

    assert result.provider == "claude"
    assert result.model == "claude-opus-5"
    assert result.prompt_tokens == 4321
    assert result.completion_tokens == 765
    assert result.duration_ms is not None
    assert result.analysis.document_type.value == "invoice"
    assert result.analysis.fields[0].key == "invoice_total"


async def test_a_thinking_block_before_the_json_is_skipped() -> None:
    """With thinking on, content[0] is not the JSON - indexing it would break."""
    analyzer, _ = _analyzer(
        _Response(
            content=[
                _Block("thinking", "Let me read the invoice..."),
                _Block("text", json.dumps(ANALYSIS_JSON)),
            ]
        )
    )

    result = await analyzer.analyze("text", filename="a.pdf", content_type="application/pdf")
    assert result.analysis.summary.startswith("An invoice from Acme")


def test_first_text_block_ignores_non_text_content() -> None:
    assert _first_text_block(_Response(content=[_Block("thinking", "x")])) is None
    assert _first_text_block(_Response(content=[])) is None


# ------------------------------------------------------------ response handling
async def test_a_refusal_is_raised_not_parsed() -> None:
    """A safety decline is HTTP 200 with stop_reason=refusal, not an exception."""

    class _Details:
        category = "cyber"

    analyzer, _ = _analyzer(
        _Response(content=[], stop_reason="refusal", stop_details=_Details())
    )

    with pytest.raises(AIProviderError) as excinfo:
        await analyzer.analyze("text", filename="a.pdf", content_type="application/pdf")
    assert "declined" in excinfo.value.message
    assert excinfo.value.details["category"] == "cyber"


async def test_a_truncated_response_is_reported_as_truncated() -> None:
    """Hitting max_tokens yields invalid JSON; the cause must be named."""
    analyzer, _ = _analyzer(
        _Response(content=[_Block("text", '{"document_type": "inv')], stop_reason="max_tokens")
    )

    with pytest.raises(AIProviderError) as excinfo:
        await analyzer.analyze("text", filename="a.pdf", content_type="application/pdf")
    assert "cut off" in excinfo.value.message


async def test_malformed_json_is_a_provider_error_not_a_crash() -> None:
    analyzer, _ = _analyzer(_Response(content=[_Block("text", "not json at all")]))

    with pytest.raises(AIProviderError) as excinfo:
        await analyzer.analyze("text", filename="a.pdf", content_type="application/pdf")
    assert "malformed JSON" in excinfo.value.message


async def test_out_of_range_values_degrade_one_field_not_the_document() -> None:
    """The whole reason this adapter coerces instead of parsing strictly."""
    payload = {
        **ANALYSIS_JSON,
        "confidence": 140,  # a percentage, and out of range
        "document_type": "not_a_real_type",
        "entities": [{"text": "Acme", "type": "invented_type", "confidence": None}],
    }
    analyzer, _ = _analyzer(_Response(content=[_Block("text", json.dumps(payload))]))

    result = await analyzer.analyze("text", filename="a.pdf", content_type="application/pdf")

    assert result.analysis.confidence == 1.0
    assert result.analysis.document_type.value == "other"
    assert result.analysis.entities[0].type.value == "other"


async def test_truncated_input_adds_a_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "LLM_MAX_INPUT_CHARS", 50)
    analyzer, _ = _analyzer(_Response(content=[_Block("text", json.dumps(ANALYSIS_JSON))]))

    result = await analyzer.analyze(
        "x" * 500, filename="long.pdf", content_type="application/pdf"
    )
    assert any("truncated" in warning for warning in result.analysis.warnings)


# ----------------------------------------------------------------------- answer
async def test_answer_reports_when_the_document_lacks_the_answer() -> None:
    analyzer, _ = _analyzer(
        _Response(
            content=[
                _Block(
                    "text",
                    json.dumps(
                        {
                            "answer": "The document does not state a due date.",
                            "answer_found": False,
                            "quotes": [],
                        }
                    ),
                )
            ]
        )
    )

    result = await analyzer.answer_question("text", "When is it due?", filename="a.pdf")
    assert result.answer_found is False
    assert result.provider == "claude"


async def test_answer_found_requires_a_non_empty_answer() -> None:
    """A model that claims success but returns nothing has not found anything."""
    analyzer, _ = _analyzer(
        _Response(
            content=[_Block("text", json.dumps({"answer": "   ", "answer_found": True, "quotes": []}))]
        )
    )

    result = await analyzer.answer_question("text", "When?", filename="a.pdf")
    assert result.answer_found is False


# ------------------------------------------------------------ error translation
def test_sdk_errors_map_onto_domain_errors() -> None:
    import httpx2 as httpx

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    auth = _translate_error(
        anthropic.AuthenticationError(
            "bad key",
            response=httpx.Response(401, request=request),
            body=None,
        )
    )
    assert isinstance(auth, AIProviderError)
    assert auth.code == "ai_auth_failed"

    limited = _translate_error(
        anthropic.RateLimitError(
            "slow down",
            response=httpx.Response(429, request=request),
            body=None,
        )
    )
    assert isinstance(limited, RateLimitedError)

    connection = _translate_error(anthropic.APIConnectionError(request=request))
    assert isinstance(connection, AIProviderUnavailableError)

    unknown = _translate_error(ValueError("something odd"))
    assert isinstance(unknown, AIProviderError)
