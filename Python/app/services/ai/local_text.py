"""Dependency-free text extraction: PDF text layer and plain text.

This is the default stage-1 engine, and the reason the document feature runs out
of the box with no AWS account. It handles the two cases that cover most real
input:

* **PDFs with a text layer** (anything produced digitally - invoices from
  accounting software, exported reports, contracts) via ``pypdf``. No OCR is
  needed; the characters are already in the file.
* **Plain text / CSV**, decoded defensively.

It deliberately does *not* handle scanned images - that genuinely requires OCR,
and pretending otherwise would return empty text and a confusing "success".
Images route to Textract when configured; otherwise the upload fails fast with
an actionable message.
"""
from __future__ import annotations

import io
import logging
import time

import anyio

from app.core.exceptions import ExtractionError
from app.services.ai.base import TextExtractionResult

logger = logging.getLogger(__name__)

PDF_TYPES = frozenset({"application/pdf"})
TEXT_TYPES = frozenset({"text/plain", "text/csv", "text/markdown", "application/json"})

# Fallback order for text with no byte-order mark. UTF-16 is deliberately absent:
# without a BOM it happily "decodes" almost any even-length byte string by pairing
# bytes, producing plausible-looking CJK garbage and stealing every cp1252 file.
# It is only used when a BOM actually proves the encoding (see _decode_text).
_TEXT_ENCODINGS = ("utf-8", "cp1252", "latin-1")

# Byte-order marks, longest first so UTF-32 is not misread as UTF-16.
_BOMS: tuple[tuple[bytes, str], ...] = (
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xfe\xff", "utf-16-be"),
    (b"\xff\xfe", "utf-16-le"),
)


class LocalTextExtractor:
    """Extracts text from PDFs (text layer) and plain-text files."""

    name = "local"

    def supports(self, content_type: str) -> bool:
        base = content_type.split(";")[0].strip().lower()
        return base in PDF_TYPES or base in TEXT_TYPES

    async def extract(
        self, data: bytes, *, content_type: str, filename: str
    ) -> TextExtractionResult:
        base = content_type.split(";")[0].strip().lower()
        started = time.perf_counter()

        if base in PDF_TYPES:
            # pypdf is CPU-bound and fully synchronous; a 200-page parse would
            # block the event loop for seconds if run inline.
            text, pages, warnings = await anyio.to_thread.run_sync(
                self._read_pdf, data
            )
        elif base in TEXT_TYPES:
            text, warnings = self._decode_text(data)
            pages = 1
        else:
            raise ExtractionError(
                f"The local text engine cannot read '{content_type}'. "
                "Enable AWS Textract (TEXTRACT_ENABLED=true) to process scanned "
                "images, or upload a PDF with a text layer.",
                details={"content_type": content_type},
            )

        duration_ms = int((time.perf_counter() - started) * 1000)
        normalised = _normalise_whitespace(text)

        if not normalised.strip():
            raise ExtractionError(
                "No text layer was found in this file. It is most likely a scan, "
                "which needs OCR - enable AWS Textract to process it.",
                details={"filename": filename, "pages": pages},
            )

        return TextExtractionResult(
            text=normalised,
            provider=self.name,
            page_count=pages,
            duration_ms=duration_ms,
            warnings=warnings,
        )

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _read_pdf(data: bytes) -> tuple[str, int, list[str]]:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError

        warnings: list[str] = []
        try:
            reader = PdfReader(io.BytesIO(data))
        except PdfReadError as exc:
            raise ExtractionError(f"The PDF could not be parsed: {exc}") from exc

        if reader.is_encrypted:
            # An empty user password is common for "print-protected" PDFs and is
            # legitimately decryptable; a real password is not our business.
            try:
                if reader.decrypt("") == 0:
                    raise ExtractionError(
                        "The PDF is password protected and cannot be read."
                    )
            except ExtractionError:
                raise
            except Exception as exc:
                raise ExtractionError(
                    "The PDF is encrypted and could not be decrypted."
                ) from exc

        pages: list[str] = []
        for index, page in enumerate(reader.pages, start=1):
            try:
                pages.append(page.extract_text() or "")
            except Exception as exc:  # one malformed page must not fail the file
                warnings.append(f"Page {index} could not be read ({type(exc).__name__}).")
                pages.append("")

        # Page markers give the analyser positional context ("the total is on the
        # last page") and let the UI cite a page number.
        body = "\n\n".join(
            f"[page {index}]\n{content.strip()}"
            for index, content in enumerate(pages, start=1)
            if content.strip()
        )
        return body, len(reader.pages), warnings

    @staticmethod
    def _decode_text(data: bytes) -> tuple[str, list[str]]:
        """Decode text bytes, trusting a BOM when present and sniffing otherwise."""
        for bom, encoding in _BOMS:
            if data.startswith(bom):
                try:
                    # The `-sig`/`utf-16`/`utf-32` codecs strip the BOM themselves.
                    return data.decode(encoding), []
                except (UnicodeDecodeError, LookupError):
                    break

        for encoding in _TEXT_ENCODINGS:
            try:
                return data.decode(encoding), []
            except (UnicodeDecodeError, LookupError):
                continue

        # latin-1 cannot fail, so reaching here is essentially impossible; keep the
        # lossy path anyway rather than rejecting a file outright.
        return data.decode("utf-8", errors="replace"), [
            "The file's encoding could not be determined; some characters may be wrong."
        ]


def _normalise_whitespace(text: str) -> str:
    """Collapse the whitespace noise typical of PDF extraction.

    PDF text layers routinely emit trailing spaces and long runs of blank lines,
    which waste prompt tokens (and therefore money) without adding information.
    """
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    out: list[str] = []
    blanks = 0
    for line in lines:
        if line:
            blanks = 0
            out.append(line)
        else:
            blanks += 1
            if blanks <= 1:  # keep single blank lines: they mark paragraphs
                out.append("")
    return "\n".join(out).strip()
