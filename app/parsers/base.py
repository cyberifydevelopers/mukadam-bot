from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ExtractedInvoice:
    txn_date: str | None = None
    amount: str | None = None
    sender_name: str | None = None
    receiver_name: str | None = None
    reference_number: str | None = None
    extra: dict = field(default_factory=dict)  # anything bank-specific, kept for the raw JSON column


class InvoiceParser(Protocol):
    """Each bank gets one implementation of this. There is no single layout
    that works across banks, so a shared parser cannot be assumed — this is
    the extension point where a bank-specific implementation is plugged in
    once you have real sample PDFs from that bank to build/test it against.
    """

    def parse(self, pdf_text: str) -> ExtractedInvoice: ...
