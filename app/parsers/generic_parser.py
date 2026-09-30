import re

from app.parsers.base import ExtractedInvoice

_AMOUNT_RE = re.compile(r"(?:INR|Rs\.?|₹)\s?([\d,]+\.\d{2}|[\d,]+)", re.IGNORECASE)
_DATE_RE = re.compile(r"\b(\d{1,2}[-/](?:\d{1,2}|[A-Za-z]{3})[-/]\d{2,4})\b")
_REF_RE = re.compile(r"(?:Ref(?:erence)?\.?\s*(?:No\.?)?|UTR|Txn\s*ID)\s*[:\-]?\s*([A-Za-z0-9]+)", re.IGNORECASE)


class GenericInvoiceParser:
    """Best-effort, layout-agnostic regex pass over the PDF's raw text.

    This exists ONLY so the pipeline can be exercised end-to-end in
    development before real bank-specific parsers are written. It has not
    been validated against any real bank statement/advice layout and should
    not be trusted for production data — every invoice it produces should be
    treated as a candidate for manual review, not a verified extraction.
    """

    def parse(self, pdf_text: str) -> ExtractedInvoice:
        amount_match = _AMOUNT_RE.search(pdf_text)
        date_match = _DATE_RE.search(pdf_text)
        ref_match = _REF_RE.search(pdf_text)

        return ExtractedInvoice(
            txn_date=date_match.group(1) if date_match else None,
            amount=amount_match.group(1) if amount_match else None,
            reference_number=ref_match.group(1) if ref_match else None,
            extra={"note": "extracted by GenericInvoiceParser fallback, unverified"},
        )
