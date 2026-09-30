"""Optional per-bank settings.

Which bank a document belongs to is decided by the LLM from the document
itself (app/parsers/llm_parser.py, run by app/graph/invoice_graph.py) — any
bank works without being listed here. Add a BankProfile only when a bank
needs something the LLM can't supply:

- `pdf_password_env`: its PDFs are password-protected (every configured
  password is tried on a locked PDF — app/services/document_reader.py).
- `parser`: a hand-written rule-based parser to use instead of the LLM
  for its documents (see app/parsers/generic_parser.py for the shape).
- a fixed `bank_code` (the Sheet tab name) — the LLM's bank name is
  matched against `display_name` / `bank_code` (bank_identity()).

`sender_match` is kept for the legacy sender-based lookup (resolve_bank())
and is not needed for classification.
"""

import re
from dataclasses import dataclass

from app.parsers.base import InvoiceParser


@dataclass(frozen=True)
class BankProfile:
    bank_code: str  # short code, also used as that bank's tab name in the Google Sheet
    display_name: str
    sender_match: tuple[str, ...]  # substrings matched against the email's From address
    parser: InvoiceParser | None = None  # None = use the shared LLM parser (see module docstring)
    pdf_password_env: str | None = None  # name of an env var holding the PDF password, if protected
    # Other spellings the LLM may give the bank's name, so they all land on
    # this profile's Sheet tab instead of each making a tab of its own.
    aliases: tuple[str, ...] = ()


# Example only — replace `sender_match` with the bank's real alert address
# once known. Leave `parser` unset to start with LLM extraction, or pass a
# hand-written InvoiceParser once you've written and verified one.
BANK_REGISTRY: list[BankProfile] = [
    # e-Statement PDFs are protected with the account holder's 13-digit CNIC
    # (no dashes) — for joint accounts, the primary holder's CNIC.
    BankProfile(
        bank_code="BAHL",
        display_name="Bank AL Habib",
        sender_match=("noumansubhani01@gmail.com",),
        pdf_password_env="BAHL_PDF_PASSWORD",
    ),
    BankProfile(
        bank_code="HABIBMETRO",
        display_name="Habib Metropolitan Bank",
        sender_match=("habibmetro.com",),
        aliases=("Habib Metro Bank", "HabibMetro", "Habib Metro", "HMB"),
    ),
    # BankProfile(
    #     bank_code="HDFC",
    #     display_name="HDFC Bank",
    #     sender_match=("alerts@hdfcbank.net",),
    #     pdf_password_env="HDFC_PDF_PASSWORD",
    # ),
]


# Email domains of banks whose name doesn't contain "bank" — used by the
# no-LLM pre-check on attachment-less emails (app/pipeline.py
# _mentions_bank()), which otherwise looks for the word "bank". Habib Metro's
# staff write from @habibmetro.com with no "bank" anywhere in the email.
# Add a domain here if a bank's emails are being skipped as "doesn't mention
# a bank".
BANK_EMAIL_DOMAINS: tuple[str, ...] = (
    "habibmetro.com",
    "hbl.com",
    "ubl.com.pk",
    "mcb.com.pk",
    "mcbislamic.com",
    "nbp.com.pk",
    "bop.com.pk",
    "jsbl.com",
    "dibpak.com",
    "albaraka.com.pk",
    "sc.com",
    "citi.com",
    "hsbc.com",
    "ztbl.com.pk",
    "fwbl.com.pk",
    "bok.com.pk",
    "sindhbank.com.pk",
    "samba.com.pk",
)


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower().replace("limited", "").replace("ltd", ""))


def bank_identity(bank_name: str | None, bank_code: str | None) -> tuple[str, str]:
    """(bank_code, display name) for a bank the LLM identified. Reuses a
    registered profile's code when the name or code matches one (so its
    PDF password / custom parser / existing Sheet tab carry over);
    otherwise derives a code from the LLM's. The code is the Sheet tab name."""
    name_n = _norm(bank_name or "")
    code_n = _norm(bank_code or "")
    for profile in BANK_REGISTRY:
        p_code = _norm(profile.bank_code)
        p_names = [_norm(n) for n in (profile.display_name, *profile.aliases)]
        if (code_n and code_n == p_code) or (
            name_n and any(name_n == p or name_n in p or p in name_n for p in p_names)
        ):
            return profile.bank_code, profile.display_name

    code = re.sub(r"[^A-Z0-9]", "", (bank_code or "").upper())[:12]
    if not code and bank_name:
        # "Meezan Bank" → "MB"… prefer the first word when it's distinctive.
        words = re.sub(r"[^A-Za-z0-9 ]", "", bank_name).split()
        code = (words[0] if words and len(words[0]) >= 3 else "".join(w[0] for w in words)).upper()[:12]
    return (code or "UNKNOWN"), (bank_name or code or "Unknown bank")


def profile_by_code(bank_code: str) -> BankProfile:
    for profile in BANK_REGISTRY:
        if profile.bank_code == bank_code:
            return profile
    return FALLBACK_PROFILE


def resolve_bank(sender_address: str) -> BankProfile | None:
    sender_lower = sender_address.lower()
    for profile in BANK_REGISTRY:
        if any(token.lower() in sender_lower for token in profile.sender_match):
            return profile
    return None


# Returned by profile_by_code() for a bank with no BankProfile — the usual
# case, since any bank the LLM identifies is accepted. No parser: its
# documents are read by the LLM.
FALLBACK_PROFILE = BankProfile(
    bank_code="UNKNOWN",
    display_name="Unknown bank",
    sender_match=(),
    parser=None,
    pdf_password_env=None,
)
