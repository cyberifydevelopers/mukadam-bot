"""The LLM document reader: one forced tool call per attachment that both
decides whether it's a bank document (and which bank) and extracts its
fields — reference, date, amount, parties, accounts, purpose and any line-
items table. It judges from the document alone (text, or the page images
for a scanned PDF), never from the wording of the email it came in. An
email with no attachment is itself the document (parse_email()): it is
judged by who wrote the original message at the bottom of any forward
chain.

Routed through OpenRouter (an OpenAI-compatible Chat Completions API) so
LLM_EXTRACTION_MODEL can point at any model OpenRouter serves — Claude,
GPT, Gemini, etc. — via a single OPENROUTER_API_KEY, using the standard
`openai` Python SDK pointed at OpenRouter's base URL.

Because this is financial data, the output is never trusted blindly:
`extra["amount_sanity_check"]` records whether a plain regex scan of the
same text independently agrees with the LLM's amount, and a mismatch is
shown in the WhatsApp confirmation prompt. The human answering YES is still
what actually authorizes the Sheets write.
"""

import base64
import json
import logging
import os
import re

from openai import APIError, OpenAI

from app.config import settings
from app.parsers.base import ExtractedInvoice

logger = logging.getLogger(__name__)

_FUNCTION = {
    "name": "record_invoice_fields",
    "description": (
        "Record the fields extracted from a bank document (payment advice, transfer "
        "receipt, invoice, delivery order, bank letter, …). Use null for any field "
        "not actually present — never invent, estimate, or infer a value that isn't "
        "written there. Not every document has a money amount (e.g. a delivery order "
        "of stock lists quantities) — then amount is null and the quantities go in "
        "items_table."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            # Decided first (the model fills fields in order): who issued it.
            "issuer_name": {
                "type": ["string", "null"],
                "description": (
                    "The organisation that ISSUED the document — whose letterhead / logo / name is "
                    "at the top as the sender (NOT the addressee: a letter 'To: The Manager, "
                    "Meezan Bank' written by a company is issued by that company)"
                ),
            },
            "issuer_is_bank": {
                "type": "boolean",
                "description": "True only if issuer_name is a bank (e.g. Meezan Bank, HBL, Bank AL Habib, MCB, UBL)",
            },
            "is_bank_document": {
                "type": "boolean",
                "description": (
                    "True only if the document was ISSUED BY A BANK — on the bank's own letterhead "
                    "(payment advice, transfer receipt, deposit slip, bank challan, remittance "
                    "advice, delivery order, L/C or guarantee document, bank letter, statement). "
                    "False when it is issued by anyone else, EVEN IF it is addressed to a bank or "
                    "mentions a bank — e.g. a customer's request letter to a bank on the "
                    "customer's own letterhead, a company invoice, a quotation, a CV."
                ),
            },
            "classification_reason": {
                "type": "string",
                "description": (
                    "One short sentence on why this is or isn't a bank document, naming whose "
                    "letterhead it is on — based only on what the document itself shows"
                ),
            },
            "txn_date": {
                "type": ["string", "null"],
                "description": "Transaction/payment date, exactly as written in the text",
            },
            "amount": {
                "type": ["string", "null"],
                "description": "Money amount of the transaction — digits and decimal point only, no currency symbol or thousands separators; null if the document states no money amount",
            },
            "sender_name": {
                "type": ["string", "null"],
                "description": "Name of the payer / remitter / sender (for a non-payment document: the party on whose behalf / account it is issued)",
            },
            "receiver_name": {
                "type": ["string", "null"],
                "description": "Name of the payee / beneficiary / receiver (for a letter or order: the addressee)",
            },
            "reference_number": {
                "type": ["string", "null"],
                "description": "Reference / UTR / transaction id / invoice number",
            },
            "document_type": {
                "type": ["string", "null"],
                "description": "What the document is, in a few words, e.g. 'IBFT transfer receipt', 'payment advice', 'deposit slip'",
            },
            "bank_name": {
                "type": ["string", "null"],
                "description": "The bank that ISSUED it (its letterhead), as written (e.g. 'Meezan Bank', 'HBL', 'Bank AL Habib'); null if not issued by a bank — a bank that is only the addressee does not count",
            },
            "currency": {
                "type": ["string", "null"],
                "description": "Currency code of the amount (e.g. PKR, USD), if shown",
            },
            "sender_account": {
                "type": ["string", "null"],
                "description": "Payer's account number / IBAN, as written",
            },
            "receiver_account": {
                "type": ["string", "null"],
                "description": "Payee's account number / IBAN, as written",
            },
            "purpose": {
                "type": ["string", "null"],
                "description": "Purpose / narration / description / subject of the document, if stated",
            },
            "items_table": {
                "type": ["object", "null"],
                "description": (
                    "The document's main table of line items, if it has one (goods, stocks, L/Cs, "
                    "invoices being paid, …), copied exactly — same column headers, one entry per "
                    "row, values as written including units (e.g. '7.560 MT'). Leave out total/"
                    "summary rows. When it has several tables of the same shape (e.g. one per "
                    "disbursement date), merge them into this one table with an extra first column "
                    "saying which one each row came from (e.g. 'Disbursement': '17 Aug 2026'). "
                    "null if the document has no such table."
                ),
                "properties": {
                    "columns": {"type": "array", "items": {"type": "string"}},
                    "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
                },
                "required": ["columns", "rows"],
            },
            "is_account_statement": {
                "type": "boolean",
                "description": (
                    "True if the document is a bank ACCOUNT STATEMENT — a list of an account's "
                    "transactions over a period with a running balance (e-statement, account "
                    "activity report). These are not tracked. False for an invoice, advice, "
                    "receipt, L/C, delivery order, letter or any single-transaction document"
                ),
            },
            "concerns_our_company": {
                "type": ["boolean", "null"],
                "description": (
                    "True if the document is addressed to, or is about goods / an account / a "
                    "transaction of, the company named as 'our company' in the instructions "
                    "(including its email address); false if it is for someone else; null if no "
                    "company is named in the instructions"
                ),
            },
        },
        "required": [
            "issuer_name",
            "issuer_is_bank",
            "is_bank_document",
            "classification_reason",
            "txn_date",
            "amount",
            "sender_name",
            "receiver_name",
            "reference_number",
            "document_type",
            "bank_name",
            "currency",
            "sender_account",
            "receiver_account",
            "purpose",
            "items_table",
        ],
    },
}

_EXTRA_FIELDS = ("issuer_name", "issuer_is_bank", "classification_reason", "document_type", "bank_name", "currency", "sender_account", "receiver_account", "purpose", "items_table", "is_account_statement", "concerns_our_company")


def _our_company_note() -> str:
    """Tells the model who 'our company' is (OUR_COMPANY_NAMES), for
    concerns_our_company — it's a check, not a reason to reject."""
    names = settings.our_company_list
    if not names:
        return ""
    return f"\n\nOur company (for concerns_our_company): {', '.join(names)}.\n\n"

_TOOLS = [{"type": "function", "function": _FUNCTION}]
_TOOL_CHOICE = {"type": "function", "function": {"name": "record_invoice_fields"}}

_SANITY_AMOUNT_RE = re.compile(r"(?:PKR|INR|Rs\.?|₹)\s?([\d,]+\.\d{2}|[\d,]+)", re.IGNORECASE)


_EMAIL_INSTRUCTION = (
    "Below is an email (its headers and body) — often a chain of forwards. Judge it by the "
    "ORIGINAL message at the bottom of the chain (the innermost 'Forwarded message' / "
    "'From:' block), not by the people who forwarded it on. That original message's author "
    "is the issuer: it is a bank document only if that author writes for a bank — a bank "
    "email domain (e.g. @meezanbank.com, @habibmetro.com, @hbl.com), a bank officer's "
    "signature, or the bank's disclaimer — and the message is the bank's business with its "
    "client: an instruction, advice or notice about a transaction, delivery, disbursement, "
    "payment, L/C or import/collection documents, pledge / stock, or the account (e.g. "
    "'kindly take the delivery as per below details', 'the consignment documents are ready, "
    "please arrange to collect'). Newsletters, promotions, OTP / login / security alerts, personal or business "
    "chat, and a customer's own email to a bank are NOT bank documents. The issuing bank is "
    "the original author's bank (e.g. @habibmetro.com → 'Habib Metropolitan Bank'). If no "
    "bank can be identified — no bank domain, name, signature or logo — it is NOT a bank "
    "document, however bank-like the wording (e.g. 'Trade Services'). Extract "
    "the fields from that "
    "original message (receiver = who it was addressed to). Use null for anything not "
    "actually present — do not guess or estimate.\n\n"
)

_EMAIL_IMAGES_INSTRUCTION = (
    "The email's {n} picture(s) follow the text — signature logos, pasted screenshots, "
    "photos or scans. Look at every one: a bank's logo (in a signature or on a letterhead) "
    "identifies the bank even when the text never names it, and a picture of a bank "
    "document (advice, receipt, delivery order, statement) is part of the email's content — "
    "extract its fields as if it were attached. Mention in classification_reason when a "
    "logo or picture decided it.\n\n"
)

_IMAGE_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"}


def _image_part(path: str) -> dict:
    """A saved picture as a Chat Completions image input (data URI)."""
    mime = _IMAGE_MIME.get(os.path.splitext(path)[1].lower(), "image/png")
    with open(path, "rb") as f:
        encoded = base64.standard_b64encode(f.read()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}


class LlmParseError(Exception):
    pass


_client: OpenAI | None = None


def openrouter_client() -> OpenAI:
    """Shared OpenRouter client (OpenAI-compatible API)."""
    global _client
    if not settings.openrouter_api_key:
        raise LlmParseError("OPENROUTER_API_KEY not configured — required for LLM classification/extraction")
    if _client is None:
        _client = OpenAI(
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            max_retries=4,  # SDK default is 2; connection drops have been common on this network
        )
    return _client


class LlmInvoiceParser:
    def _client_or_raise(self) -> OpenAI:
        return openrouter_client()

    def parse(self, pdf_text: str, image_paths: list[str] | tuple = ()) -> ExtractedInvoice:
        """`image_paths`: pictures inside the document (a Word file's
        letterhead logo), looked at along with its text."""
        instruction = (
            "Extract the payment fields from this bank payment advice / "
            "invoice (text of a PDF or Word file, or the cells of a spreadsheet, "
            "one row per line). Use null for anything not actually present — "
            "do not guess or estimate.\n\n"
        )
        if image_paths:
            instruction += (
                f"The document's {len(image_paths)} picture(s) follow the text — a logo on its "
                "letterhead shows who issued it even when the text doesn't name them.\n\n"
            )
        return self._parse_text(instruction, pdf_text, method="llm_text", image_paths=image_paths)

    def parse_email(self, email_text: str, image_paths: list[str] | tuple = ()) -> ExtractedInvoice:
        """An email judged as a whole — its text plus its pictures (signature
        logos, pasted screenshots, photos / scans of a document). The content
        is often in the body (e.g. a bank's delivery instruction, forwarded a
        few times before reaching us), and the bank often recognisable only
        by its logo."""
        instruction = _EMAIL_INSTRUCTION
        if image_paths:
            instruction += _EMAIL_IMAGES_INSTRUCTION.format(n=len(image_paths))
        return self._parse_text(instruction, email_text, method="llm_email", image_paths=image_paths)

    def _parse_text(
        self, instruction: str, text: str, method: str, image_paths: list[str] | tuple = ()
    ) -> ExtractedInvoice:
        client = self._client_or_raise()
        content: str | list = instruction + _our_company_note() + text[:15000]
        if image_paths:
            content = [{"type": "text", "text": content}, *(_image_part(p) for p in image_paths)]

        try:
            response = client.chat.completions.create(
                model=settings.llm_extraction_model,
                temperature=0,
                tools=_TOOLS,
                tool_choice=_TOOL_CHOICE,
                messages=[{"role": "user", "content": content}],
            )
        except APIError as exc:
            raise LlmParseError(f"OpenRouter API call failed: {exc}") from exc

        return self._to_extracted_invoice(response, sanity_check_text=text, method=method)

    def parse_from_pdf_bytes(self, pdf_bytes: bytes) -> ExtractedInvoice:
        """Fallback for a PDF with no usable text layer (scanned pages,
        or a layout pdfplumber can't read cleanly) — hands the actual pages
        to the model as a native PDF file input instead of raw text, via
        OpenRouter's file-input support (`plugins: [{"id": "file-parser"}]`
        with engine "native" so a model with its own PDF vision support,
        such as Claude, reads the pages directly rather than OpenRouter
        pre-converting them). This only benefits banks on the default LLM
        parser and a model that actually supports native PDF input — check
        the chosen LLM_EXTRACTION_MODEL's capabilities on openrouter.ai if
        results look off.
        """
        client = self._client_or_raise()
        encoded = base64.standard_b64encode(pdf_bytes).decode("ascii")

        try:
            response = client.chat.completions.create(
                model=settings.llm_extraction_model,
                temperature=0,
                tools=_TOOLS,
                tool_choice=_TOOL_CHOICE,
                extra_body={"plugins": [{"id": "file-parser", "pdf": {"engine": "native"}}]},
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "file",
                                "file": {
                                    "filename": "invoice.pdf",
                                    "file_data": f"data:application/pdf;base64,{encoded}",
                                },
                            },
                            {
                                "type": "text",
                                "text": (
                                    "Extract the payment fields from this bank payment "
                                    "advice / invoice document. Use null for anything not "
                                    "actually present — do not guess or estimate."
                                    + _our_company_note()
                                ),
                            },
                        ],
                    }
                ],
            )
        except APIError as exc:
            raise LlmParseError(f"OpenRouter API call failed: {exc}") from exc

        # No text layer to regex against here, so the sanity check can only
        # report "no_regex_match" (uncorroborated, not necessarily wrong) —
        # that's surfaced to the human reviewer the same way a text-mode
        # mismatch is, via app/pipeline.py's WhatsApp prompt.
        return self._to_extracted_invoice(response, sanity_check_text="", method="llm_vision")

    def _to_extracted_invoice(self, response, sanity_check_text: str, method: str) -> ExtractedInvoice:
        message = response.choices[0].message
        tool_calls = message.tool_calls
        if not tool_calls:
            raise LlmParseError("Model did not return the expected tool call")

        try:
            fields = json.loads(tool_calls[0].function.arguments)
        except (json.JSONDecodeError, AttributeError, IndexError) as exc:
            raise LlmParseError(f"Could not parse tool call arguments: {exc}") from exc

        amount = fields.get("amount")

        return ExtractedInvoice(
            txn_date=fields.get("txn_date"),
            amount=amount,
            sender_name=fields.get("sender_name"),
            receiver_name=fields.get("receiver_name"),
            reference_number=fields.get("reference_number"),
            extra={
                "extraction_method": method,
                "llm_model": settings.llm_extraction_model,
                "amount_sanity_check": self._sanity_check_amount(sanity_check_text, amount),
                "is_bank_document": fields.get("is_bank_document", True),
                **{k: fields.get(k) for k in _EXTRA_FIELDS},
            },
        )

    @staticmethod
    def _sanity_check_amount(pdf_text: str, llm_amount: str | None) -> str:
        match = _SANITY_AMOUNT_RE.search(pdf_text)
        if not match:
            return "no_regex_match"  # can't corroborate either way — not necessarily wrong

        regex_amount = match.group(1).replace(",", "")
        normalized_llm = (llm_amount or "").replace(",", "")
        return "match" if regex_amount == normalized_llm else "mismatch"
