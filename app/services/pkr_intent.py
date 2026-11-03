"""What a message says about the rupee side of a document in dollars, in whatever words it is written ("rate is 280",
"dollar 281.5 ka hai", "PKR 1.4 million came to", Urdu, Roman Urdu…). The fixed forms in openwa_service.py are only the quick
path; everything else is read by the AI model with the waiting document in front of it, so no wording has to be listed.

Used for WhatsApp replies (app/routers/openwa_webhook.py); written so a mailed-in rate can use the same reading."""

import json
import logging

from app.models import InvoiceRecord

logger = logging.getLogger(__name__)

_SYSTEM = """You read one message from the person who approves bank documents. A document in US dollars is waiting for its rupee side, shown below. Decide what the message gives, whatever the wording or language (English, Urdu, Roman Urdu):
- "dollar_rate": rupees for one US dollar (the exchange rate), e.g. "rate is 280", "dollar 281.5 ka hai", "today's usd closing is 279".
- "pkr_amount": the whole amount of this document in rupees.
- "other": anything else: a question, the rate / price / margin / weight / drums of goods, yes or no, a request for a report, chit-chat.
A rate with no goods named is the dollar rate, because that is the figure being waited for. A rate for named goods (wheat, cotton, oil…) is "other". When unsure, answer "other".
Reply with JSON only: {"kind": "dollar_rate" | "pkr_amount" | "other", "rate": number or null, "amount": number or null}. Plain numbers, no commas; rate only for dollar_rate, amount only for pkr_amount."""


def _document_facts(record: InvoiceRecord) -> dict:
    extra = json.loads(record.raw_extracted_json or "{}")
    order = extra.get("delivery_order") or {}
    return {
        "id": record.id,
        "document_type": extra.get("document_type"),
        "customer": order.get("customer_name"),
        "amount": record.amount,
        "currency": extra.get("currency"),
        "goods": [row.get("description") for row in order.get("rows") or [] if row.get("description")],
    }


def _number(value) -> float | None:
    try:
        number = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def understand(text: str, record: InvoiceRecord) -> dict | None:
    """{"kind": "dollar_rate" | "pkr_amount" | "other", "rate", "amount"} for `text`, said about the USD document
    `record` that waits for its rupee side. None when the AI service can't be used or answers nonsense, so the caller
    can fall back on what it has."""
    from app.parsers import llm_parser  # late: the parser module is heavy and imports settings at load

    try:
        client = llm_parser.openrouter_client()
        response = llm_parser.chat_with_fallback(
            client,
            temperature=0,
            max_tokens=120,
            timeout=30,
            messages=[
                {"role": "system", "content": _SYSTEM + "\n\nWAITING DOCUMENT:\n" + json.dumps(_document_facts(record), ensure_ascii=False)},
                {"role": "user", "content": text},
            ],
        )
        raw = (response.choices[0].message.content or "").strip()
        data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])  # tolerates a code fence around the JSON
    except Exception:  # noqa: BLE001 — a provider failure or a malformed answer is "can't tell", not a crash
        logger.warning("Could not read %r as a dollar rate / rupee amount", text, exc_info=True)
        return None

    rate, amount = _number(data.get("rate")), _number(data.get("amount"))
    if data.get("kind") == "dollar_rate" and rate:
        return {"kind": "dollar_rate", "rate": rate, "amount": None}
    if data.get("kind") == "pkr_amount" and amount:
        return {"kind": "pkr_amount", "rate": None, "amount": amount}
    return {"kind": "other", "rate": None, "amount": None}
