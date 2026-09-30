"""Cross-checks on a document the LLM accepted as a bank document. The
document's own letterhead (checked by the LLM) is what decides bank / not
bank; these checks verify it against the email it came in, and are shown
on the WhatsApp prompt and in the Sheet — a failed check is a warning, not
a rejection (the approver decides):

1. Sender: did the document's bank actually send it? Some sender in the
   email's forward chain must be from that bank's email domain (e.g. a
   Meezan Bank PDF with …@meezanbank.com somewhere in the chain).
2. Our company: is the document addressed to / about us (OUR_COMPANY_NAMES
   in .env), not some unrelated party?
"""

import json
import re

from app.bank_registry import BANK_EMAIL_DOMAINS, profile_by_code
from app.config import settings
from app.models import InvoiceRecord
from app.parsers.base import ExtractedInvoice

# Free mail providers are never a bank's domain.
_FREE_MAIL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "live.com", "icloud.com", "aol.com", "proton.me"}


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower().replace("limited", "").replace("ltd", ""))


def _domain(address: str) -> str:
    return address.rsplit("@", 1)[-1].lower()


def _is_bank_address(address: str) -> bool:
    domain = _domain(address)
    if domain in _FREE_MAIL:
        return False
    return "bank" in domain or any(domain == d or domain.endswith("." + d) for d in BANK_EMAIL_DOMAINS)


def _address_of_bank(address: str, bank_code: str, bank_name: str | None) -> bool:
    """Whether this sender writes for the document's bank: a registered
    sender pattern, or the domain's name part matching the bank's name
    (meezanbank.com ↔ "Meezan Bank", habibmetro.com ↔ "Habib Metropolitan")."""
    if not _is_bank_address(address):
        return False
    profile = profile_by_code(bank_code)
    if any(token.lower() in address for token in profile.sender_match):
        return True
    root = _norm(_domain(address).split(".")[0])
    names = [n for n in (bank_name, profile.display_name, bank_code, *profile.aliases) if n and n != "Unknown bank"]
    return any(root and (root in _norm(n) or _norm(n) in root) for n in names if len(_norm(n)) >= 3)


def check_sender(record: InvoiceRecord) -> dict:
    senders = json.loads(record.email.chain_senders or "[]") if record.email else []
    bank = record.bank_name or record.bank_code
    from_bank = [a for a in senders if _address_of_bank(a, record.bank_code, record.bank_name)]
    if from_bank:
        return {"ok": True, "text": f"Sent by {bank}: {from_bank[-1]}"}
    other_banks = [a for a in senders if _is_bank_address(a)]
    if other_banks:
        return {"ok": False, "text": f"Sent by another bank ({other_banks[-1]}), not {bank}"}
    return {"ok": False, "text": f"Not sent by {bank} - no bank address in the email ({', '.join(senders[-2:]) or 'no sender'})"}


def check_our_company(extracted: ExtractedInvoice, text: str | None) -> dict | None:
    """None when OUR_COMPANY_NAMES isn't set."""
    names = settings.our_company_list
    if not names:
        return None
    extra = extracted.extra
    haystack = _norm(
        " ".join(
            str(v or "")
            for v in (text, extracted.receiver_name, extracted.sender_name, extra.get("purpose"), json.dumps(extra.get("items_table") or {}))
        )
    )
    found = next((n for n in names if _norm(n) and _norm(n) in haystack), None)
    if found:
        return {"ok": True, "text": f"Addressed to us: {found}"}
    if extra.get("concerns_our_company"):
        return {"ok": True, "text": "Addressed to us (per the document)"}
    return {"ok": False, "text": f"Doesn't mention {' / '.join(names[:2])}"}


def verify(record: InvoiceRecord, extracted: ExtractedInvoice, text: str | None) -> list[dict]:
    checks = [check_sender(record), check_our_company(extracted, text)]
    return [c for c in checks if c]


def check_lines(extra: dict) -> list[str]:
    """✓ / ⚠ lines for the WhatsApp prompt and the Sheet."""
    return [("✓ " if c["ok"] else "⚠ ") + c["text"] for c in extra.get("checks") or []]
