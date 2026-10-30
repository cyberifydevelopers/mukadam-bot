"""Documents in US dollars: the bank has them in USD, our reports and the sheet are in rupees. The approver is asked for the
PKR amount (or the dollar rate) in the WhatsApp prompt and answers with e.g. "pkr amount 1500000" or "usd rate 280"; what
they give is kept on the record (raw_extracted_json["pkr"] = {"rate", "amount"}) and, for a delivery order, as its
"usd_pkr_rate", which the stock report converts its rates by. USD_PKR_RATE in the settings is the rate used when none was given.
"""

import json
import re

from sqlalchemy.orm import Session

from app.config import settings
from app.models import InvoiceRecord, InvoiceStatus

_USD = {"USD", "US$", "$", "US DOLLAR", "US DOLLARS", "DOLLAR", "DOLLARS"}


def is_usd(extra: dict | None) -> bool:
    return ((extra or {}).get("currency") or "").strip().upper() in _USD


def number(text) -> float | None:
    """'5,000.00' → 5000.0; None when it isn't a plain number."""
    match = re.fullmatch(r"\s*(?:usd|us\$|\$)?\s*(\d[\d,]*(?:\.\d+)?)\s*", str(text or ""), re.IGNORECASE)
    return float(match.group(1).replace(",", "")) if match else None


def rate_for(extra: dict | None) -> float:
    """PKR per dollar for this document: what the approver gave, else the settings' USD_PKR_RATE, else 0 (unknown)."""
    given = ((extra or {}).get("pkr") or {}).get("rate")
    return float(given) if given else float(settings.usd_pkr_rate or 0)


def pkr_amount(record: InvoiceRecord, extra: dict | None) -> float | None:
    """The amount in rupees: given by the approver, else the dollar amount × the rate; None when neither is known."""
    given = ((extra or {}).get("pkr") or {}).get("amount")
    if given:
        return float(given)
    usd, rate = number(record.amount), rate_for(extra)
    return round(usd * rate, 2) if usd and rate else None


def prompt_lines(record: InvoiceRecord, extra: dict) -> list[str]:
    """What the YES/NO prompt says about a USD document: the rupee amount, or a request for it."""
    amount, rate = pkr_amount(record, extra), rate_for(extra)
    if amount:
        return [f"💱 *In USD.* PKR amount: *PKR {amount:,.0f}*" + (f" (at Rs {rate:g} per dollar)" if rate else "")]
    return [
        "💱 *This amount is in USD* — the bank has it in dollars.",
        "Please send the *PKR amount*, e.g. `pkr amount 1500000`, or the dollar rate, e.g. `usd rate 280`.",
    ]


def currency_cell(extra: dict, record: InvoiceRecord) -> str | None:
    """The sheet's currency column: 'USD', or 'USD → PKR 1,400,000 (at 280)' once the rupee amount is known."""
    currency = extra.get("currency")
    amount = pkr_amount(record, extra) if is_usd(extra) else None
    if not amount:
        return currency
    rate = rate_for(extra)
    return f"{currency} → PKR {amount:,.0f}" + (f" (at {rate:g})" if rate else "")


def apply(record: InvoiceRecord, rate: float | None = None, amount: float | None = None) -> dict | None:
    """Keeps what the approver gave: the rate, or the rupee amount (then the rate is amount ÷ dollars). Returns
    {"rate", "amount"} as stored, or None when the dollar amount is needed and can't be read."""
    extra = json.loads(record.raw_extracted_json or "{}")
    usd = number(record.amount)
    if rate is None and amount is not None:
        if not usd:
            return None
        rate = amount / usd
    if rate is None or rate <= 0:
        return None
    if amount is None and usd:
        amount = usd * rate
    given = {"rate": round(rate, 4), "amount": round(amount, 2) if amount else None}
    extra["pkr"] = given
    if extra.get("delivery_order"):
        extra["delivery_order"]["usd_pkr_rate"] = given["rate"]  # the stock report converts its rates by this
    record.raw_extracted_json = json.dumps(extra)
    return given


def latest_usd_record(db: Session) -> InvoiceRecord | None:
    """The newest document in USD still without a rupee amount — what a bare "usd rate 280" is taken to be about."""
    for record in db.query(InvoiceRecord).filter(
        InvoiceRecord.status.in_((InvoiceStatus.PENDING_CONFIRMATION, InvoiceStatus.CONFIRMED))
    ).order_by(InvoiceRecord.id.desc()).limit(40):
        extra = json.loads(record.raw_extracted_json or "{}")
        if is_usd(extra) and not (extra.get("pkr") or {}).get("rate"):
            return record
    return None
