"""Unit tests for the Textract adapter.

Textract needs live AWS credentials, so the boto3 client is stubbed. What is
worth testing without the service is exactly what we own: reassembling readable
text from LINE blocks, walking the two-level KEY_VALUE_SET relationship graph
that Textract's FORMS feature returns, and mapping AWS error codes onto
actionable messages.

The block payloads below mirror the real ``AnalyzeDocument`` response shape.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.core.exceptions import AIProviderError, ExtractionError
from app.services.ai.textract import (
    TextractExtractor,
    _extract_key_values,
    _lines_to_text,
    _translate_error,
)


def line(block_id: str, text: str, page: int = 1) -> dict[str, Any]:
    return {"Id": block_id, "BlockType": "LINE", "Text": text, "Page": page}


def word(block_id: str, text: str) -> dict[str, Any]:
    return {"Id": block_id, "BlockType": "WORD", "Text": text}


def key_value(
    block_id: str, *, entity: str, child_ids: list[str], value_ids: list[str] | None = None
) -> dict[str, Any]:
    relationships: list[dict[str, Any]] = [{"Type": "CHILD", "Ids": child_ids}]
    if value_ids:
        relationships.append({"Type": "VALUE", "Ids": value_ids})
    return {
        "Id": block_id,
        "BlockType": "KEY_VALUE_SET",
        "EntityTypes": [entity],
        "Relationships": relationships,
    }


# ------------------------------------------------------------------ line rebuild
def test_lines_are_grouped_by_page_in_order() -> None:
    blocks = [
        line("3", "second page line", page=2),
        line("1", "first line", page=1),
        line("2", "second line", page=1),
        {"Id": "x", "BlockType": "PAGE"},  # non-LINE blocks are ignored
        {"Id": "y", "BlockType": "WORD", "Text": "ignored"},
    ]
    text, pages = _lines_to_text(blocks)

    assert pages == 2
    assert text.index("[page 1]") < text.index("[page 2]")
    assert "first line\nsecond line" in text
    assert "ignored" not in text


def test_no_lines_yields_no_text() -> None:
    assert _lines_to_text([{"Id": "1", "BlockType": "PAGE"}]) == ("", None)


def test_blocks_without_a_page_default_to_page_one() -> None:
    blocks = [{"Id": "1", "BlockType": "LINE", "Text": "only line"}]
    text, pages = _lines_to_text(blocks)
    assert pages == 1
    assert "only line" in text


# --------------------------------------------------------------- form key/values
def test_key_value_pairs_are_reassembled_across_relationships() -> None:
    """A field is a KEY block -> VALUE block -> WORD children; all three must join."""
    blocks = [
        word("w1", "Invoice"),
        word("w2", "Number:"),
        word("w3", "INV-2024-00871"),
        key_value("k1", entity="KEY", child_ids=["w1", "w2"], value_ids=["v1"]),
        key_value("v1", entity="VALUE", child_ids=["w3"]),
    ]
    assert _extract_key_values(blocks) == [
        {"key": "Invoice Number", "value": "INV-2024-00871"}  # trailing colon stripped
    ]


def test_a_key_with_no_value_still_reports_the_label() -> None:
    blocks = [
        word("w1", "Signature"),
        key_value("k1", entity="KEY", child_ids=["w1"]),
    ]
    assert _extract_key_values(blocks) == [{"key": "Signature", "value": ""}]


def test_ticked_checkboxes_are_not_silently_dropped() -> None:
    """A selected checkbox carries no text, so the state has to be rendered."""
    blocks = [
        word("w1", "Agreed"),
        key_value("k1", entity="KEY", child_ids=["w1"], value_ids=["v1"]),
        key_value("v1", entity="VALUE", child_ids=["s1"]),
        {"Id": "s1", "BlockType": "SELECTION_ELEMENT", "SelectionStatus": "SELECTED"},
    ]
    assert _extract_key_values(blocks) == [{"key": "Agreed", "value": "[x]"}]


def test_unselected_checkboxes_produce_an_empty_value() -> None:
    blocks = [
        word("w1", "Agreed"),
        key_value("k1", entity="KEY", child_ids=["w1"], value_ids=["v1"]),
        key_value("v1", entity="VALUE", child_ids=["s1"]),
        {"Id": "s1", "BlockType": "SELECTION_ELEMENT", "SelectionStatus": "NOT_SELECTED"},
    ]
    assert _extract_key_values(blocks) == [{"key": "Agreed", "value": ""}]


def test_value_blocks_are_not_mistaken_for_keys() -> None:
    blocks = [
        word("w1", "Total"),
        word("w2", "500"),
        key_value("k1", entity="KEY", child_ids=["w1"], value_ids=["v1"]),
        key_value("v1", entity="VALUE", child_ids=["w2"]),
    ]
    pairs = _extract_key_values(blocks)
    assert len(pairs) == 1
    assert pairs[0]["key"] == "Total"


def test_a_dangling_value_reference_does_not_crash() -> None:
    """Textract pagination can split a document mid-relationship."""
    blocks = [
        word("w1", "Orphan"),
        key_value("k1", entity="KEY", child_ids=["w1"], value_ids=["missing-id"]),
    ]
    assert _extract_key_values(blocks) == [{"key": "Orphan", "value": ""}]


# ----------------------------------------------------------------- supports/route
def test_supports_only_the_formats_textract_handles() -> None:
    extractor = TextractExtractor(region="us-east-1")
    assert extractor.supports("application/pdf")
    assert extractor.supports("image/png")
    assert extractor.supports("image/jpeg; charset=binary")  # parameters tolerated
    assert not extractor.supports("text/plain")
    assert not extractor.supports("application/zip")


async def test_unsupported_type_raises_before_any_aws_call() -> None:
    extractor = TextractExtractor(region="us-east-1")
    with pytest.raises(ExtractionError, match="does not support"):
        await extractor.extract(b"data", content_type="application/zip", filename="a.zip")


async def test_a_large_pdf_without_s3_fails_with_an_actionable_message() -> None:
    """The async API is the only multi-page path and it reads only from S3."""
    from app.core.config import settings

    extractor = TextractExtractor(region="us-east-1", staging_bucket=None)
    oversized = b"%PDF-" + b"x" * (settings.TEXTRACT_MAX_SYNC_BYTES + 1)

    with pytest.raises(ExtractionError, match="S3_BUCKET"):
        await extractor.extract(
            oversized, content_type="application/pdf", filename="big.pdf"
        )


async def test_sync_extraction_uses_the_forms_feature() -> None:
    """FORMS is requested because label-to-box geometry is lost in flat text."""
    captured: dict[str, Any] = {}

    class FakeTextract:
        def analyze_document(self, **kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {
                "Blocks": [
                    line("l1", "Invoice Number: INV-1"),
                    word("w1", "Invoice"),
                    word("w2", "Number:"),
                    word("w3", "INV-1"),
                    key_value("k1", entity="KEY", child_ids=["w1", "w2"], value_ids=["v1"]),
                    key_value("v1", entity="VALUE", child_ids=["w3"]),
                ]
            }

    extractor = TextractExtractor(region="us-east-1")
    extractor._client = FakeTextract()

    result = await extractor.extract(
        b"%PDF-fake", content_type="application/pdf", filename="a.pdf"
    )

    assert captured["FeatureTypes"] == ["FORMS", "TABLES"]
    assert result.provider == "textract"
    assert "Invoice Number: INV-1" in result.text
    assert result.detected_fields == [{"key": "Invoice Number", "value": "INV-1"}]
    assert result.duration_ms is not None


async def test_empty_textract_output_is_an_error_not_a_silent_success() -> None:
    class FakeTextract:
        def analyze_document(self, **kwargs: Any) -> dict[str, Any]:
            return {"Blocks": []}

    extractor = TextractExtractor(region="us-east-1")
    extractor._client = FakeTextract()

    with pytest.raises(ExtractionError, match="no text"):
        await extractor.extract(b"%PDF-x", content_type="application/pdf", filename="a.pdf")


# ------------------------------------------------------------- error translation
class _ClientError(Exception):
    """Mimics botocore's ClientError, which carries a structured response dict."""

    def __init__(self, code: str) -> None:
        super().__init__(f"An error occurred ({code})")
        self.response = {"Error": {"Code": code, "Message": "boom"}}


_ClientError.__name__ = "ClientError"


@pytest.mark.parametrize(
    ("code", "expected_type", "fragment"),
    [
        ("UnsupportedDocumentException", ExtractionError, "unreadable"),
        ("BadDocumentException", ExtractionError, "unreadable"),
        ("DocumentTooLargeException", ExtractionError, "size limit"),
        ("AccessDeniedException", AIProviderError, "IAM"),
        ("ThrottlingException", AIProviderError, "throttling"),
        ("SomeOtherException", AIProviderError, "failed"),
    ],
)
def test_aws_error_codes_map_to_actionable_messages(
    code: str, expected_type: type, fragment: str
) -> None:
    translated = _translate_error(_ClientError(code))
    assert isinstance(translated, expected_type)
    assert fragment.lower() in translated.message.lower()


def test_missing_credentials_are_reported_clearly() -> None:
    class NoCredentialsError(Exception):
        pass

    translated = _translate_error(NoCredentialsError("no creds"))
    assert isinstance(translated, AIProviderError)
    assert "AWS_ACCESS_KEY_ID" in translated.message
