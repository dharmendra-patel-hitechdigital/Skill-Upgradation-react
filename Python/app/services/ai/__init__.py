"""AI provider layer.

Everything here sits behind the two protocols in :mod:`app.services.ai.base`.
The pipeline imports the protocols and the registry - never a vendor SDK.
"""
from app.services.ai.base import (
    AnalysisResult,
    AnswerResult,
    DocumentAnalyzer,
    TextExtractionResult,
    TextExtractor,
)
from app.services.ai.registry import (
    ProviderStatus,
    describe_providers,
    get_analyzer,
    get_analyzer_with_fallback,
    get_text_extractor,
)

__all__ = [
    "AnalysisResult",
    "AnswerResult",
    "DocumentAnalyzer",
    "ProviderStatus",
    "TextExtractionResult",
    "TextExtractor",
    "describe_providers",
    "get_analyzer",
    "get_analyzer_with_fallback",
    "get_text_extractor",
]
