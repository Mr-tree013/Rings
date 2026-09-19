"""Minimal generated PDF fixtures, so PDF tests need no writer dependency.

`build_text_pdf` writes a real, valid PDF by hand (catalog, pages tree, one Helvetica text
line per page, correct xref table). Encryption reuses pypdf, which is already a production
dependency.
"""

from __future__ import annotations

from collections.abc import Sequence
from io import BytesIO

import pypdf


def build_text_pdf(pages: Sequence[str]) -> bytes:
    """Build a valid PDF with one text line on each page."""
    font_number = 3 + 2 * len(pages)
    objects: dict[int, bytes] = {}
    kids: list[str] = []
    for index, text in enumerate(pages):
        page_number = 3 + 2 * index
        content_number = page_number + 1
        kids.append(f"{page_number} 0 R")
        objects[page_number] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_number} 0 R >> >> "
            f"/Contents {content_number} 0 R >>"
        ).encode("ascii")
        stream = f"BT /F1 14 Tf 72 700 Td ({_pdf_escape(text)}) Tj ET".encode("ascii")
        objects[content_number] = (
            b"<< /Length "
            + str(len(stream)).encode("ascii")
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[2] = (
        f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>"
    ).encode("ascii")
    objects[font_number] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n".encode("ascii")
        out += objects[number]
        out += b"\nendobj\n"
    xref_offset = len(out)
    size = max(objects) + 1
    out += f"xref\n0 {size}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for number in range(1, size):
        out += f"{offsets.get(number, 0):010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n"
    ).encode("ascii")
    return bytes(out)


def build_encrypted_pdf(pages: Sequence[str], *, password: str = "secret") -> bytes:
    """Build the same PDF, encrypted with a user password we never try to guess."""
    reader = pypdf.PdfReader(BytesIO(build_text_pdf(pages)))
    writer = pypdf.PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt(user_password=password, owner_password=password)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def malformed_pdf() -> bytes:
    """Bytes that claim to be a PDF but are not one."""
    return b"%PDF-1.4\nthis is not a real pdf body\n"


def _pdf_escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


__all__ = ["build_encrypted_pdf", "build_text_pdf", "malformed_pdf"]

