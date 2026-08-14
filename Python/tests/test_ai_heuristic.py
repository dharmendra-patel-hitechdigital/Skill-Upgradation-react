"""Unit tests for the rule-based analyser and the local text extractor.

This engine is the guarantee that the AI feature works with no credentials, so it
gets tested on its own merits rather than only through the HTTP layer.
"""
from __future__ import annotations

import pytest

from app.core.exceptions import ExtractionError
from app.schemas.document import DocumentKind, EntityType
from app.services.ai.heuristic import HeuristicAnalyzer
from app.services.ai.local_text import LocalTextExtractor
from tests.pdf_builder import CONTRACT_LINES, INVOICE_LINES, build_pdf, invoice_pdf

INVOICE_TEXT = "\n".join(INVOICE_LINES)
CONTRACT_TEXT = "\n".join(CONTRACT_LINES)

RESUME_TEXT = """
JORDAN ELLIS
Senior Backend Engineer
jordan.ellis@example.com | +1 415 555 0132

PROFESSIONAL EXPERIENCE
Staff Engineer, Cloudbank Systems Inc, 2020 - present
Built distributed payment services handling 4000 requests per second.

Backend Engineer, Riverstone Technologies, 2017 - 2020

EDUCATION
BSc Computer Science, University of Washington, 2017

SKILLS
Python, FastAPI, PostgreSQL, Kubernetes, distributed systems

CERTIFICATIONS
AWS Certified Solutions Architect
"""

STATEMENT_TEXT = """
NORTHSHORE BANK
Account Statement

Account Number: 40125566
Statement Period: 01/03/2024 to 31/03/2024
Opening Balance: 4200.00
Closing Balance: 3865.50

Transactions
05/03/2024  Card payment  -120.00
11/03/2024  Deposit        500.00
"""


# ------------------------------------------------------------- classification
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (INVOICE_TEXT, DocumentKind.INVOICE),
        (CONTRACT_TEXT, DocumentKind.CONTRACT),
        (RESUME_TEXT, DocumentKind.RESUME),
        (STATEMENT_TEXT, DocumentKind.STATEMENT),
    ],
)
async def test_documents_are_classified_by_their_signals(
    text: str, expected: DocumentKind
) -> None:
    result = await HeuristicAnalyzer().analyze(
        text, filename="document.pdf", content_type="application/pdf"
    )
    assert result.analysis.document_type is expected


async def test_unremarkable_text_is_not_force_classified() -> None:
    """One weak keyword must not be enough - a wrong label is worse than 'other'."""
    result = await HeuristicAnalyzer().analyze(
        "The quick brown fox jumped over the lazy dog several times this morning.",
        filename="notes.txt",
        content_type="text/plain",
    )
    assert result.analysis.document_type is DocumentKind.OTHER


# --------------------------------------------------------------------- fields
async def test_invoice_fields_are_extracted() -> None:
    result = await HeuristicAnalyzer().analyze(
        INVOICE_TEXT, filename="invoice.pdf", content_type="application/pdf"
    )
    fields = {field.key: field.value for field in result.analysis.fields}

    assert fields["invoice_number"] == "INV-2024-00871"
    assert fields["invoice_date"] == "14/03/2024"
    assert fields["due_date"] == "13/04/2024"
    assert fields["purchase_order"] == "PO-55231"
    assert "1488.00" in fields["total_amount"]
    assert "1240.00" in fields["subtotal"]


async def test_totals_without_a_colon_are_still_found() -> None:
    """Real invoices often lay totals out as columns, not `Label: value`."""
    result = await HeuristicAnalyzer().analyze(
        "ORDER SUMMARY\nSubtotal    900.00\nTax (VAT)    180.00\nGRAND TOTAL   1080.00\n",
        filename="receipt.txt",
        content_type="text/plain",
    )
    fields = {field.key: field.value for field in result.analysis.fields}
    assert "1080.00" in fields["total_amount"]
    assert "900.00" in fields["subtotal"]
    assert "180.00" in fields["tax_amount"]


async def test_prose_containing_a_colon_is_not_treated_as_a_field() -> None:
    """A long label is a sentence, not a form field."""
    result = await HeuristicAnalyzer().analyze(
        "Please note the following important consideration: the terms may change.\n",
        filename="note.txt",
        content_type="text/plain",
    )
    assert result.analysis.fields == []


# ------------------------------------------------------------------- entities
async def test_entities_are_typed_and_deduplicated() -> None:
    result = await HeuristicAnalyzer().analyze(
        INVOICE_TEXT, filename="invoice.pdf", content_type="application/pdf"
    )
    entities = result.analysis.entities
    by_type: dict[EntityType, set[str]] = {}
    for entity in entities:
        by_type.setdefault(entity.type, set()).add(entity.text)

    assert "accounts@northwind-trading.example" in by_type[EntityType.EMAIL]
    assert any("2024" in date for date in by_type[EntityType.DATE])
    # Bare amounts on money-labelled lines must be caught, not just $-prefixed ones.
    assert "1488.00" in by_type[EntityType.MONEY]
    assert any("Acme" in org for org in by_type.get(EntityType.ORGANIZATION, set()))

    # No exact duplicates of the same (text, type).
    pairs = [(entity.text, entity.type) for entity in entities]
    assert len(pairs) == len(set(pairs))


async def test_currency_symbols_are_detected_as_money() -> None:
    result = await HeuristicAnalyzer().analyze(
        "Payment of $1,250.00 and £300 received. Contact ops@example.com.",
        filename="note.txt",
        content_type="text/plain",
    )
    money = {e.text for e in result.analysis.entities if e.type is EntityType.MONEY}
    assert any("1,250.00" in value for value in money)


async def test_bare_numbers_outside_a_money_context_are_not_money() -> None:
    """Version numbers and quantities must not be reported as amounts."""
    result = await HeuristicAnalyzer().analyze(
        "The build version is 12.45 and the sample size was 30.00 units observed.",
        filename="report.txt",
        content_type="text/plain",
    )
    money = {e.text for e in result.analysis.entities if e.type is EntityType.MONEY}
    assert money == set()


# ------------------------------------------------------- summary, language, etc
async def test_summary_is_extractive_and_bounded() -> None:
    result = await HeuristicAnalyzer().analyze(
        CONTRACT_TEXT, filename="nda.pdf", content_type="application/pdf"
    )
    summary = result.analysis.summary
    assert summary
    assert len(summary) <= 4000
    # Extractive means every sentence really came from the document.
    assert "NON-DISCLOSURE" in summary.upper() or "agreement" in summary.lower()


async def test_language_detection() -> None:
    english = await HeuristicAnalyzer().analyze(
        "The company shall deliver the goods to the buyer at the agreed address "
        "and the buyer shall pay for them within thirty days of the invoice date. "
        "This is the entire understanding of the parties in this matter.",
        filename="a.txt",
        content_type="text/plain",
    )
    assert english.analysis.language == "en"


async def test_language_is_none_when_there_is_no_signal() -> None:
    """Guessing on OCR noise or a table of numbers would be worse than admitting it."""
    result = await HeuristicAnalyzer().analyze(
        "4711 2298 3310\n99.00 12.50 8.75\n", filename="a.txt", content_type="text/plain"
    )
    assert result.analysis.language is None


async def test_confidence_is_capped_below_a_real_model() -> None:
    """A rule engine must not claim model-grade confidence - downstream uses this."""
    result = await HeuristicAnalyzer().analyze(
        INVOICE_TEXT, filename="invoice.pdf", content_type="application/pdf"
    )
    assert 0.05 <= result.analysis.confidence <= 0.78


async def test_the_engine_declares_itself_in_the_warnings() -> None:
    """A consumer must be able to tell a rule-based result from a model one."""
    result = await HeuristicAnalyzer().analyze(
        INVOICE_TEXT, filename="invoice.pdf", content_type="application/pdf"
    )
    assert any("OPENAI_API_KEY" in warning for warning in result.analysis.warnings)
    assert result.provider == "heuristic"


async def test_tiny_documents_are_flagged() -> None:
    result = await HeuristicAnalyzer().analyze(
        "Hi.", filename="a.txt", content_type="text/plain"
    )
    assert any("very little text" in warning for warning in result.analysis.warnings)


# --------------------------------------------------------------- question answer
async def test_question_answering_finds_supported_passages() -> None:
    result = await HeuristicAnalyzer().answer_question(
        INVOICE_TEXT, "What is the invoice number?", filename="invoice.pdf"
    )
    assert result.answer_found is True
    assert "INV-2024-00871" in " ".join(result.quotes)


async def test_question_answering_admits_ignorance() -> None:
    result = await HeuristicAnalyzer().answer_question(
        INVOICE_TEXT, "Which penguin species nests here?", filename="invoice.pdf"
    )
    assert result.answer_found is False


async def test_question_answering_on_empty_text() -> None:
    result = await HeuristicAnalyzer().answer_question("", "Anything?", filename="a.txt")
    assert result.answer_found is False
    assert result.answer


# ------------------------------------------------------------- local extractor
async def test_pdf_text_layer_is_extracted_with_page_markers() -> None:
    extractor = LocalTextExtractor()
    result = await extractor.extract(
        invoice_pdf(), content_type="application/pdf", filename="invoice.pdf"
    )
    assert result.provider == "local"
    assert result.page_count == 1
    assert "[page 1]" in result.text
    assert "INV-2024-00871" in result.text


async def test_multi_page_pdf_reports_every_page() -> None:
    extractor = LocalTextExtractor()
    data = build_pdf([["Page one content here"], ["Page two content here"]])
    result = await extractor.extract(
        data, content_type="application/pdf", filename="two.pdf"
    )
    assert result.page_count == 2
    assert "[page 1]" in result.text and "[page 2]" in result.text


async def test_a_pdf_with_no_text_layer_raises_an_actionable_error() -> None:
    extractor = LocalTextExtractor()
    with pytest.raises(ExtractionError, match="[Tt]extract"):
        await extractor.extract(
            build_pdf([[""]]), content_type="application/pdf", filename="scan.pdf"
        )


async def test_a_corrupt_pdf_is_reported_not_crashed() -> None:
    extractor = LocalTextExtractor()
    with pytest.raises(ExtractionError):
        await extractor.extract(
            b"%PDF-1.4\nthis is not a real pdf body",
            content_type="application/pdf",
            filename="broken.pdf",
        )


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "cp1252"])
async def test_text_files_are_decoded_across_encodings(encoding: str) -> None:
    extractor = LocalTextExtractor()
    result = await extractor.extract(
        "Dear Ms Fauré, the café invoice is attached.".encode(encoding),
        content_type="text/plain",
        filename="note.txt",
    )
    assert "invoice" in result.text


async def test_images_are_not_claimed_by_the_local_extractor() -> None:
    """It must decline rather than return empty text for a scan."""
    extractor = LocalTextExtractor()
    assert not extractor.supports("image/png")
    with pytest.raises(ExtractionError, match="[Tt]extract"):
        await extractor.extract(
            b"\x89PNG\r\n\x1a\n", content_type="image/png", filename="scan.png"
        )


async def test_whitespace_is_normalised() -> None:
    """Runs of blank lines waste prompt tokens without adding information."""
    extractor = LocalTextExtractor()
    result = await extractor.extract(
        b"Line one   \n\n\n\n\n\nLine two\t\t\n",
        content_type="text/plain",
        filename="messy.txt",
    )
    assert "\n\n\n" not in result.text
    assert result.text == "Line one\n\nLine two"
