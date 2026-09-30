import io
import logging
import os

import pdfplumber
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError

logger = logging.getLogger(__name__)


class PdfExtractionError(Exception):
    pass


class NoTextLayerError(PdfExtractionError):
    """Raised specifically when the PDF opened fine but has no extractable
    text — i.e. it's a scanned/image-only PDF. Distinguished from a generic
    PdfExtractionError so callers can decide to retry via a vision-capable
    LLM call (app/parsers/llm_parser.py `parse_from_pdf_bytes`) instead of
    just failing.
    """


def extract_text(pdf_path: str, password: str | None = None) -> str:
    """Returns all text content of a PDF, decrypting it first if a password
    is supplied. Many Indian bank e-statements/payment advices ship as
    password-protected PDFs (commonly PAN, customer ID, or name+DOB — this
    varies per bank and must be configured per BankProfile, it cannot be
    guessed), so password support is load-bearing, not optional.
    """
    if not os.path.exists(pdf_path):
        raise PdfExtractionError(f"PDF not found: {pdf_path}")

    try:
        with pdfplumber.open(pdf_path, password=password or "") as pdf:
            text_parts = [page.extract_text() or "" for page in pdf.pages]
    except Exception as exc:  # pdfplumber/pdfminer raise varied exception types on bad password/corrupt file
        raise PdfExtractionError(f"Failed to open/parse {pdf_path}: {exc}") from exc

    full_text = "\n".join(text_parts).strip()
    if not full_text:
        raise NoTextLayerError(
            f"No extractable text in {pdf_path} — likely a scanned/image-only PDF or one that is "
            "entirely paragraph/prose formatted with no embedded text layer."
        )
    return full_text


def read_decrypted_bytes(pdf_path: str, password: str | None = None) -> bytes:
    """Returns the PDF's raw bytes with any password protection stripped —
    for handing the actual pages to a vision-capable model (Claude's native
    PDF document input) when there's no usable text layer to extract, e.g.
    a scanned bank document. Claude can read the pages directly but cannot
    itself remove a password, so decryption still has to happen here first.
    """
    if not os.path.exists(pdf_path):
        raise PdfExtractionError(f"PDF not found: {pdf_path}")

    try:
        reader = PdfReader(pdf_path)
        if reader.is_encrypted:
            if reader.decrypt(password or "") == 0:
                raise PdfExtractionError(f"Incorrect password for {pdf_path}")

        if not reader.is_encrypted:
            with open(pdf_path, "rb") as f:
                return f.read()

        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        buffer = io.BytesIO()
        writer.write(buffer)
        return buffer.getvalue()
    except PdfReadError as exc:
        raise PdfExtractionError(f"Failed to open/decrypt {pdf_path}: {exc}") from exc
