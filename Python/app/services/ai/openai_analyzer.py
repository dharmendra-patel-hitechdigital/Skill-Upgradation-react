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
from app.schemas.document import DocumentAnalysis
from app.services.ai.analysis_contract import (
    ANALYSIS_SYSTEM_PROMPT,
    ANSWER_SYSTEM_PROMPT,
    MAX_QUOTES,
    MAX_SUMMARY_CHARS,
    analysis_schema,
    answer_schema,
    build_analysis_prompt,
    build_answer_prompt,
    clamp_confidence,
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

# The prompts, the JSON Schema and the coercion pass are shared with every other
# LLM adapter - see analysis_contract for why they do not live here. Re-exported
# under the previous private names so existing callers and tests keep working.
_analysis_schema = analysis_schema
_answer_schema = answer_schema
_clamp_confidence = clamp_confidence
_coerce_analysis = coerce_analysis
_MAX_SUMMARY_CHARS = MAX_SUMMARY_CHARS
_MAX_QUOTES = MAX_QUOTES


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

        user_content = build_analysis_prompt(
            prompt_text, filename=filename, content_type=content_type, truncated=truncated
        )

        started = time.perf_counter()
        payload, usage = await self._complete(
            system=ANALYSIS_SYSTEM_PROMPT,
            user=user_content,
            schema_name="document_analysis",
            schema=_analysis_schema(),
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
            schema_name="document_answer",
            schema=_answer_schema(),
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
