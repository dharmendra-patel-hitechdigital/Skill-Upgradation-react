"""Anthropic Claude adapter for document understanding and grounded Q&A.

The sibling of :mod:`app.services.ai.openai_analyzer`: same protocol, same
prompts, same JSON Schema, same coercion pass (all in
:mod:`app.services.ai.analysis_contract`). Only the SDK call and the error
translation differ, which is exactly the point of the ``DocumentAnalyzer``
protocol - a second provider is one file, not a second pipeline.

Three Claude-specific decisions:

**1. Structured outputs via ``output_config.format``.** The response is
constrained to our JSON Schema at decode time, so the first text block is
guaranteed to be valid JSON matching it. No fenced-code-block scraping, and "the
model answered in prose today" stops being a failure mode.

**2. Adaptive thinking, at a configurable effort.** Claude decides how much to
think per document rather than burning a fixed budget on every receipt. Effort
defaults to ``medium``: document extraction is largely mechanical, and the
default (``high``) buys little here while costing real latency on a queue.

**3. The raw response is coerced, not parsed straight into Pydantic.** The SDK's
``messages.parse()`` helper would validate strictly and raise on a single
out-of-range confidence, losing the whole document. The shared coercion pass
degrades one field instead - see ``analysis_contract.coerce_analysis``.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from app.core.config import settings
from app.core.exceptions import AIProviderError
from app.schemas.document import DocumentAnalysis
from app.services.ai.analysis_contract import (
    ANALYSIS_SYSTEM_PROMPT,
    ANSWER_SYSTEM_PROMPT,
    analysis_schema,
    answer_schema,
    build_analysis_prompt,
    build_answer_prompt,
    coerce_analysis,
    coerce_answer,
    truncation_warning,
)
from app.services.ai.base import (
    AnalysisResult,
    AnswerResult,
    truncate_for_model,
)

logger = logging.getLogger(__name__)


class ClaudeAnalyzer:
    """Document analysis and Q&A backed by an Anthropic Claude model."""

    name = "claude"

    def __init__(
        self,
        *,
        api_key: str,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        effort: str | None = None,
        max_output_tokens: int | None = None,
    ) -> None:
        self._api_key = api_key
        self.model = model or settings.ANTHROPIC_MODEL
        self._base_url = base_url or settings.ANTHROPIC_BASE_URL
        self._timeout = (
            timeout if timeout is not None else settings.ANTHROPIC_TIMEOUT_SECONDS
        )
        self._max_retries = (
            max_retries if max_retries is not None else settings.ANTHROPIC_MAX_RETRIES
        )
        self._effort = effort or settings.ANTHROPIC_EFFORT
        self._max_output_tokens = (
            max_output_tokens
            if max_output_tokens is not None
            else settings.ANTHROPIC_MAX_OUTPUT_TOKENS
        )
        self._client: Any = None

    def _get_client(self) -> Any:
        """Build the client once. It owns a connection pool worth reusing.

        Imported lazily so the service still starts - and the other providers
        still work - when the ``anthropic`` package is not installed.
        """
        if self._client is None:
            try:
                from anthropic import AsyncAnthropic
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise AIProviderError(
                    "The 'anthropic' package is not installed."
                ) from exc

            kwargs: dict[str, Any] = {
                "api_key": self._api_key,
                "timeout": self._timeout,
                # The SDK retries connection errors, 429s and 5xx with jittered
                # backoff. Letting it do that is strictly better than a
                # hand-rolled loop, which would also retry deterministic 400s.
                "max_retries": self._max_retries,
            }
            if self._base_url:
                kwargs["base_url"] = self._base_url
            self._client = AsyncAnthropic(**kwargs)
        return self._client

    # ------------------------------------------------------------------ analyse
    async def analyze(
        self, text: str, *, filename: str, content_type: str
    ) -> AnalysisResult:
        prompt_text, truncated = truncate_for_model(text, settings.LLM_MAX_INPUT_CHARS)
        user_content = build_analysis_prompt(
            prompt_text, filename=filename, content_type=content_type, truncated=truncated
        )

        started = time.perf_counter()
        payload, usage = await self._complete(
            system=ANALYSIS_SYSTEM_PROMPT,
            user=user_content,
            schema=analysis_schema(),
        )
        duration_ms = int((time.perf_counter() - started) * 1000)

        coerced = coerce_analysis(payload)
        if truncated:
            coerced.setdefault("warnings", []).append(truncation_warning())

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
        user_content = build_answer_prompt(
            prompt_text, question, filename=filename, truncated=truncated
        )

        payload, _ = await self._complete(
            system=ANSWER_SYSTEM_PROMPT,
            user=user_content,
            schema=answer_schema(),
        )
        answer, answer_found, quotes = coerce_answer(payload)

        return AnswerResult(
            answer=answer or "The model returned an empty answer.",
            answer_found=answer_found,
            quotes=quotes,
            provider=self.name,
            model=self.model,
        )

    # ------------------------------------------------------------------- shared
    async def _complete(
        self, *, system: str, user: str, schema: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, int | None]]:
        """One structured-output message, with errors mapped to domain errors."""
        client = self._get_client()

        request: dict[str, Any] = {
            "model": self.model,
            # Bounded by the schema itself - the analysis caps out at 25
            # keywords, 100 entities and 100 fields - so this is a real ceiling
            # rather than a guess. A response that still hits it is truncated
            # JSON, which is detected below.
            "max_tokens": self._max_output_tokens,
            # Anthropic takes the system prompt as a top-level parameter, not as
            # a message with role="system".
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_config": {
                "effort": self._effort,
                "format": {"type": "json_schema", "schema": schema},
            },
            # Adaptive: the model decides how much to think per document. A fixed
            # token budget is deprecated on current models and rejected outright
            # on the newest ones.
            "thinking": {"type": "adaptive"},
        }

        try:
            response = await client.messages.create(**request)
        except Exception as exc:
            raise _translate_error(exc) from exc

        stop_reason = getattr(response, "stop_reason", None)

        # A safety decline arrives as HTTP 200 with stop_reason="refusal", so it
        # has to be checked before reading content - not caught as an exception.
        if stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise AIProviderError(
                "The AI provider declined to process this document.",
                details={"category": str(getattr(details, "category", None) or "unknown")},
            )
        if stop_reason == "max_tokens":
            raise AIProviderError(
                "The AI response was cut off before it was complete. Try a smaller "
                "document or raise ANTHROPIC_MAX_OUTPUT_TOKENS."
            )

        content = _first_text_block(response)
        if content is None:
            raise AIProviderError("The AI provider returned no text content.")

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
            # Mapped onto the pipeline's provider-neutral names: Anthropic calls
            # these input/output, OpenAI calls them prompt/completion, and the
            # extraction record stores one pair for both.
            "prompt_tokens": getattr(usage_obj, "input_tokens", None),
            "completion_tokens": getattr(usage_obj, "output_tokens", None),
        }
        return payload, usage


def _first_text_block(response: Any) -> str | None:
    """The first ``text`` block's content.

    Iterated rather than indexed at ``content[0]``: with thinking enabled the
    response can lead with a ``thinking`` block, and ``content[0].text`` would
    raise or return reasoning instead of the JSON.
    """
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) == "text":
            return getattr(block, "text", None)
    return None


def _translate_error(exc: Exception) -> Exception:
    """Map Anthropic SDK exceptions onto domain errors with operator-useful text."""
    try:
        import anthropic
    except ImportError:  # pragma: no cover - optional dependency
        return AIProviderError(f"Claude call failed: {exc}")

    from app.core.exceptions import AIProviderUnavailableError, RateLimitedError

    if isinstance(exc, anthropic.AuthenticationError):
        return AIProviderError(
            "Anthropic rejected the API key. Check ANTHROPIC_API_KEY.",
            code="ai_auth_failed",
        )
    if isinstance(exc, anthropic.PermissionDeniedError):
        return AIProviderError(
            "This Anthropic account is not permitted to use the configured model.",
            details={"model": settings.ANTHROPIC_MODEL},
        )
    if isinstance(exc, anthropic.NotFoundError):
        return AIProviderError(
            f"The model '{settings.ANTHROPIC_MODEL}' does not exist or is "
            "unavailable to this account."
        )
    if isinstance(exc, anthropic.RateLimitError):
        # Distinguished from a transient 429 by the SDK having already retried.
        return RateLimitedError(
            "The Anthropic rate limit or quota was exhausted. Retry this document later."
        )
    if isinstance(exc, anthropic.APITimeoutError):
        return AIProviderError(
            f"Anthropic did not respond within "
            f"{settings.ANTHROPIC_TIMEOUT_SECONDS:.0f}s."
        )
    if isinstance(exc, anthropic.APIConnectionError):
        return AIProviderUnavailableError("Could not reach the Anthropic API.")
    if isinstance(exc, anthropic.BadRequestError):
        return AIProviderError(
            "Anthropic rejected the request as invalid.",
            details={"reason": str(exc)[:300]},
        )
    if isinstance(exc, anthropic.APIStatusError):
        return AIProviderError(
            f"Anthropic returned an error (HTTP {exc.status_code}).",
            details={"reason": str(exc)[:300]},
        )
    return AIProviderError(f"Unexpected Anthropic failure: {exc}")
