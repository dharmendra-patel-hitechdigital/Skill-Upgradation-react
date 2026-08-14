"""Build small, valid PDFs with a real text layer for tests.

The document pipeline's default path parses a PDF text layer with ``pypdf``.
Testing that against a hand-rolled fake would prove nothing, and committing a
binary fixture makes the input opaque. So we emit genuine PDFs here - correct
object table, correct byte offsets in the xref - from plain text supplied by the
test. The content is visible in the test that uses it, which is what you want
when a extraction assertion fails.
"""
from __future__ import annotations


def _escape(text: str) -> str:
    """Escape the three characters that are special inside a PDF string."""
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def build_pdf(pages: list[list[str]], *, font_size: int = 11) -> bytes:
    """Return a PDF where ``pages[i]`` is the list of text lines on page *i*.

    >>> data = build_pdf([["Hello world"]])
    >>> data.startswith(b"%PDF-")
    True
    """
    if not pages:
        pages = [[""]]

    objects: list[bytes] = []

    def add(body: bytes) -> int:
        """Append an object and return its 1-based object number."""
        objects.append(body)
        return len(objects)

    # Object numbers are allocated up front because the catalog, the page tree
    # and the pages all reference each other.
    catalog_num = 1
    pages_num = 2
    objects.extend([b"", b""])  # placeholders, filled in below

    font_num = add(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        b"/Encoding /WinAnsiEncoding >>"
    )

    page_nums: list[int] = []
    for lines in pages:
        # A text object: begin text, pick a font, set a leading, then one
        # show-text-and-advance (Tj / T*) per line.
        commands = [b"BT", f"/F1 {font_size} Tf".encode(), f"{font_size + 4} TL".encode(), b"72 760 Td"]
        for index, line in enumerate(lines):
            if index:
                commands.append(b"T*")
            commands.append(f"({_escape(line)}) Tj".encode("latin-1", "replace"))
        commands.append(b"ET")
        stream = b"\n".join(commands)

        content_num = add(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        )
        page_nums.append(
            add(
                b"<< /Type /Page /Parent "
                + str(pages_num).encode()
                + b" 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 "
                + str(font_num).encode()
                + b" 0 R >> >> /Contents "
                + str(content_num).encode()
                + b" 0 R >>"
            )
        )

    kids = b" ".join(f"{num} 0 R".encode() for num in page_nums)
    objects[pages_num - 1] = (
        b"<< /Type /Pages /Kids [" + kids + b"] /Count " + str(len(page_nums)).encode() + b" >>"
    )
    objects[catalog_num - 1] = b"<< /Type /Catalog /Pages " + str(pages_num).encode() + b" 0 R >>"

    # Serialise, recording each object's byte offset for the xref table. A wrong
    # offset here is exactly what makes a hand-written PDF unreadable.
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n"

    xref_offset = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n"
    out += b"0000000000 65535 f \n"  # the mandatory free entry
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        b"trailer\n<< /Size "
        + str(len(objects) + 1).encode()
        + b" /Root "
        + str(catalog_num).encode()
        + b" 0 R >>\nstartxref\n"
        + str(xref_offset).encode()
        + b"\n%%EOF\n"
    )
    return bytes(out)


INVOICE_LINES = [
    "ACME TECHNOLOGIES LTD",
    "123 Industrial Estate, Manchester, M1 4AB",
    "",
    "TAX INVOICE",
    "",
    "Invoice Number: INV-2024-00871",
    "Invoice Date: 14/03/2024",
    "Due Date: 13/04/2024",
    "Purchase Order: PO-55231",
    "",
    "Bill To: Northwind Trading Company",
    "Contact Email: accounts@northwind-trading.example",
    "Contact Phone: +44 161 496 0234",
    "",
    "Description                 Qty     Unit Price      Amount",
    "Managed hosting (annual)      1        960.00       960.00",
    "Priority support add-on      12         15.00       180.00",
    "Onboarding consultancy        4         25.00       100.00",
    "",
    "Subtotal: 1240.00",
    "Tax (VAT 20%): 248.00",
    "Total Amount: 1488.00",
    "",
    "Payment Terms: Net 30",
    "Amount Due: 1488.00",
    "Remit payment to Acme Technologies Ltd, account 40125566.",
]

CONTRACT_LINES = [
    "MUTUAL NON-DISCLOSURE AGREEMENT",
    "",
    "This Agreement is entered into as of 1 February 2024 between",
    "Blackwood Analytics Inc and Harper Logistics LLC, together the parties.",
    "",
    "1. Confidentiality. Each party shall protect the confidential information",
    "of the other party and shall not disclose it to any third party.",
    "",
    "2. Term. This agreement remains effective for three years from the",
    "effective date unless terminated earlier in writing.",
    "",
    "3. Governing Law. This agreement shall be governed by the laws of the",
    "State of Delaware.",
    "",
    "In witness whereof the parties have executed this agreement.",
]


def invoice_pdf() -> bytes:
    """A realistic single-page invoice."""
    return build_pdf([INVOICE_LINES])


def contract_pdf() -> bytes:
    """A two-page contract, for exercising multi-page extraction."""
    return build_pdf([CONTRACT_LINES[:8], CONTRACT_LINES[8:]])
