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

from openai import APIConnectionError, APIError, APITimeoutError, OpenAI

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
            # Every document in a multi-document file, so nothing on the later
            # pages is lost — shown on the WhatsApp prompt (app/pipeline.py).
            "document_details": {
                "type": ["array", "null"],
                "description": (
                    "The file often holds several documents: the bank's letter first, then what it "
                    "encloses (bill of lading, commercial invoice, packing list, certificate of "
                    "origin, insurance, weight / quality certificates, …). Read EVERY page. List each "
                    "enclosed document once, in page order — not the bank's own letter (its fields "
                    "go in the fields above), and an ORIGINAL and its COPY only once. For each, its "
                    "key details as written: document number and date, parties (seller / shipper, "
                    "buyer / consignee, notify), goods and type, quantity, net / gross weight, unit "
                    "price, amount and currency, L/C number, vessel / voyage, ports, container and "
                    "seal numbers, H.S. code, packages. A COMMERCIAL INVOICE must never be left out, "
                    "and always with all of: invoice number and date, seller, buyer, goods, H.S. code, "
                    "quantity, unit price, total amount and currency, L/C number, proforma invoice. "
                    "Leave out blank or placeholder fields (e.g. "
                    "'XXXXXXXX') and printed terms & conditions. null when the file is a single document."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "document": {"type": "string", "description": "e.g. 'Bill of Lading', 'Commercial Invoice'"},
                        "details": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {"label": {"type": "string"}, "value": {"type": "string"}},
                                "required": ["label", "value"],
                            },
                        },
                    },
                    "required": ["document", "details"],
                },
            },
            # Fills the stock report sent back after a YES (app/services/stock_report.py).
            "delivery_order": {
                "type": ["object", "null"],
                "description": (
                    "Only when the bank instructs us (the muccadam) to move pledged / financed stock "
                    "in our custody: a DELIVERY ORDER releasing stock from the godown ('Delivery order "
                    "of stocks/assets', 'kindly take the delivery as per below details'), or an L/C "
                    "endorsement letter whose goods are to be placed in a godown under our muccadam "
                    "control (goods coming in). null for anything else, e.g. a collection-of-documents "
                    "notice."
                ),
                "properties": {
                    "customer_name": {
                        "type": ["string", "null"],
                        "description": "The client whose stock it is (who pledged it), e.g. 'Canon Foam Industries (PVT) Ltd', as written",
                    },
                    "pledge_location": {
                        "type": ["string", "null"],
                        "description": "Where the stock is held (godown / pledge site), as written",
                    },
                    "bank_department": {
                        "type": ["string", "null"],
                        "description": "The issuing bank department, if shown (e.g. 'Credit Administration Department')",
                    },
                    "bank_branch": {
                        "type": ["string", "null"],
                        "description": (
                            "The issuing branch followed by its city, from the letterhead or footer "
                            "address (e.g. 'Zahoor Elahi Road Branch, Lahore') — include the city"
                        ),
                    },
                    "currency": {
                        "type": ["string", "null"],
                        "description": "Currency of rate_per_unit (e.g. 'USD', 'PKR'); null when no rate is given",
                    },
                    "reference_note": {
                        "type": ["string", "null"],
                        "description": (
                            "One short line naming the documents, for the report: the bank letter's reference "
                            "and date, then the invoice / bill of lading numbers and vessel when enclosed — e.g. "
                            "'Your letter CAD/MUL/26INSU0501-00462 dated 21-09-2026; Commercial Invoice "
                            "420028934, B/L No. 16, MV ANNE'"
                        ),
                    },
                    "rows": {
                        "type": "array",
                        "minItems": 1,
                        "description": (
                            "One entry per stock line, in order, values as written including units (e.g. "
                            "'10.710 MT', 'NIL'); null for a value the document doesn't give. Never empty: "
                            "for an L/C letter, one line per kind of goods, with the quantity actually "
                            "shipped under THIS L/C (its bill of lading / the invoice's shipped quantity, "
                            "e.g. '1,800.000 MT') as quantity_received — not the L/C's maximum ('2000 MT "
                            "minus 10 pct') and not a whole vessel's total from a surveyor's certificate"
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "description": {
                                    "type": "string",
                                    "description": (
                                        "Short (under 50 characters, it fills one table cell): the goods and "
                                        "their L/C number, e.g. 'Brazilian Soybeans (bulk) L/C 26INSU0501-00462' "
                                        "or 'L/C #-02736' as written (prefix the disbursement date when the "
                                        "document groups rows by it)"
                                    ),
                                },
                                "rate_per_unit": {
                                    "type": ["string", "null"],
                                    "description": (
                                        "Price per unit of the quantity, digits only (e.g. '496.91' for 'USD 496.91 "
                                        "/MT'), from the enclosed commercial invoice; null when no price is given "
                                        "(a delivery order has none)"
                                    ),
                                },
                                "opening_balance": {"type": ["string", "null"], "description": "Balance in hand before this delivery"},
                                "quantity_received": {
                                    "type": ["string", "null"],
                                    "description": "Quantity coming INTO the godown (an L/C's goods to be placed there); null on a delivery order",
                                },
                                "quantity_released": {
                                    "type": ["string", "null"],
                                    "description": "Quantity to be released / delivered OUT of the godown now; null for incoming goods",
                                },
                                "balance_after": {"type": ["string", "null"], "description": "Balance after delivery"},
                            },
                            "required": [
                                "description", "opening_balance", "quantity_received", "quantity_released",
                                "balance_after", "rate_per_unit",
                            ],
                        },
                    },
                },
                "required": ["customer_name", "pledge_location", "currency", "reference_note", "rows"],
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

_EXTRA_FIELDS = ("issuer_name", "issuer_is_bank", "classification_reason", "document_type", "bank_name", "currency", "sender_account", "receiver_account", "purpose", "items_table", "document_details", "delivery_order", "is_account_statement", "concerns_our_company")


def _our_company_note() -> str:
    """Tells the model who 'our company' is (OUR_COMPANY_NAMES), for
    concerns_our_company — it's a check, not a reason to reject."""
    names = settings.our_company_list
    if not names:
        return ""
    return f"\n\nOur company (for concerns_our_company): {', '.join(names)}.\n\n"

# A bank letter often has its enclosures (B/L, invoices, packing lists,
# certificates) scanned into the same file after it.
_ALL_PAGES_INSTRUCTION = (
    "The file may hold several documents — read every page, not just the first. The bank's "
    "letter gives the main fields; record each document enclosed after it in document_details."
)
# A multi-document file's text runs long; 15k characters cut off its enclosures.
_MAX_TEXT_CHARS = 60000

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


def _api_error(exc: APIError) -> LlmParseError:
    """An OpenRouter failure as a plain-language message for the dashboard —
    the people reading it aren't developers. The raw error goes to the log."""
    logger.warning("OpenRouter API call failed: %s", exc)
    status = getattr(exc, "status_code", None)
    if status == 402:
        message = (
            "The AI service has run out of credits. Please add credits to the OpenRouter "
            "account (openrouter.ai/settings/credits), then press Retry."
        )
    elif status == 401:
        message = (
            "The AI service rejected the API key — it may be wrong or expired. Please "
            "check OPENROUTER_API_KEY in the settings, then press Retry."
        )
    elif status == 429:
        message = "The AI service is busy (too many requests right now). Please wait a minute and press Retry."
    elif isinstance(exc, (APIConnectionError, APITimeoutError)):
        message = "Couldn't reach the AI service — check the internet connection, then press Retry."
    elif status is not None and status >= 500:
        message = "The AI service is having a temporary problem on its side. Please try again in a few minutes."
    else:
        message = f"The AI service returned an error ({status or 'unknown'}). Please press Retry; if it keeps happening, contact support."
    return LlmParseError(message)


_client: OpenAI | None = None


def openrouter_client() -> OpenAI:
    """Shared OpenRouter client (OpenAI-compatible API)."""
    global _client
    if not settings.openrouter_api_key:
        raise LlmParseError("The AI service isn't set up yet — OPENROUTER_API_KEY is missing from the settings.")
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
            "do not guess or estimate. " + _ALL_PAGES_INSTRUCTION + "\n\n"
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
        content: str | list = instruction + _our_company_note() + text[:_MAX_TEXT_CHARS]
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
            raise _api_error(exc) from exc

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
                                    "actually present — do not guess or estimate. "
                                    + _ALL_PAGES_INSTRUCTION
                                    + _our_company_note()
                                ),
                            },
                        ],
                    }
                ],
            )
        except APIError as exc:
            raise _api_error(exc) from exc

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
