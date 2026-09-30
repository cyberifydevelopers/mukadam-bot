"""Reads the transaction rows out of an account-statement PDF's text — the
layout where each row starts with a date and ends with `<amount> <balance>`
(Bank AL Habib e-Statement, and similar).

Statements have a single amount column, so debit vs credit is decided by
the running balance: balance went up by `amount` → credit, down → debit.
Every row is reconciled that way, which makes misreads visible instead of
silently wrong (`reconciled=False`).

Currently used for the dashboard's PDF view only; the pipeline still
stores one record per PDF (see app/parsers/llm_parser.py).
"""

import re
from dataclasses import asdict, dataclass

_AMT = r"-?[\d,]+\.\d{2}"
_DATE = r"\d{2}/\d{2}/\d{4}"
_ROW_RE = re.compile(rf"^({_DATE})\s+(.*?)\s*({_AMT})\s+({_AMT})$")
_OPENING_RE = re.compile(rf"^{_DATE}\s+Opening Balance\s+({_AMT})$", re.IGNORECASE)
_CLOSING_RE = re.compile(rf"^{_DATE}\s+Closing Balance\s+({_AMT})$", re.IGNORECASE)
# Page furniture that sits between a row and its wrapped detail lines.
_NOISE_RE = re.compile(
    rf"^({_AMT}|Carried Forward|Brought Forward.*|Page \d+ of \d+|Totals:.*)$", re.IGNORECASE
)
# The bank's footer after the last row ("Your Bank NationWide www.bankalhabib.com
# For More Information Please Call : 111-014-014") — would otherwise glue onto
# the final transaction's details.
_FOOTER_RE = re.compile(r"www\.|please call|nationwide|for more information", re.IGNORECASE)


def _num(s: str) -> float:
    return float(s.replace(",", ""))


_IBAN_RE = re.compile(r"^PK\d{2}[A-Z]{4}[0-9A-Z]{10,}$")
_ACCOUNT_RE = re.compile(r"^\d{8,}-[A-Z]{2,5}$")  # IBFT counterparty account, e.g. 02110108689715-MZB
_REF_RE = re.compile(r"^(\d{6,})\s")  # leading instrument / document number


def _party_and_reference(details: str) -> tuple[str | None, str | None]:
    """Best-effort counterparty name and reference from a row's details.
    The details text is always kept in full; these are conveniences for the
    WhatsApp message and the sheet, not authoritative fields."""
    parts = [p.strip() for p in details.split(",")]
    ref = m[1] if (m := _REF_RE.match(details)) else None

    party = None
    for i, part in enumerate(parts[:-1]):
        # RAAST / P2M rows: "..., <IBAN>, <NAME>, ..." — IBFT rows: "..., <acct>-<BANK>, <NAME>, ..."
        if _IBAN_RE.match(part) or _ACCOUNT_RE.match(part):
            party = parts[i + 1]
            break
    else:
        # Card/POS rows: "PayPak - POS DR, <MERCHANT>, ..."
        if "POS" in parts[0].upper() and len(parts) > 1:
            party = parts[1]
    if party:
        # A wrapped reference line can glue onto the name ("NEXTGEN SOLUTIONS AMEZNPKKA0582…").
        party = re.sub(r"\s+\S*\d{6,}.*$", "", party).strip()
    return (party or None), ref


@dataclass
class StatementRow:
    date: str
    details: str
    debit: float | None
    credit: float | None
    balance: float
    reconciled: bool
    party: str | None = None
    reference: str | None = None


def parse_statement(text: str) -> dict | None:
    """Returns {"opening", "closing", "rows": [...], "totals": {...}}, or None
    when the text doesn't look like a statement (no opening balance / rows)."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    opening_idx = next((i for i, l in enumerate(lines) if _OPENING_RE.match(l)), None)
    if opening_idx is None:
        return None
    # Everything above the opening balance is the page header, which repeats
    # at the top of every page and would otherwise glue onto the previous
    # page's last row as "details".
    page_header = set(lines[:opening_idx])

    opening = closing = None
    rows: list[StatementRow] = []
    prev_balance = None

    for line in lines[opening_idx:]:
        if m := _OPENING_RE.match(line):
            opening = prev_balance = _num(m[1])
            continue
        if m := _CLOSING_RE.match(line):
            closing = _num(m[1])
            continue
        if m := _ROW_RE.match(line):
            date, details, amount, balance = m[1], m[2], _num(m[3]), _num(m[4])
            debit = credit = None
            reconciled = False
            if prev_balance is not None:
                if abs(prev_balance + amount - balance) < 0.005:
                    credit, reconciled = amount, True
                elif abs(prev_balance - amount - balance) < 0.005:
                    debit, reconciled = amount, True
            rows.append(StatementRow(date, details, debit, credit, balance, reconciled))
            prev_balance = balance
            continue
        if rows and line not in page_header and not _NOISE_RE.match(line) and not _FOOTER_RE.search(line):
            rows[-1].details += " " + line  # wrapped continuation of the row's details

    if not rows:
        return None
    for row in rows:
        row.details = re.sub(r"\s+", " ", row.details).strip(" ,")
        row.party, row.reference = _party_and_reference(row.details)

    return {
        "opening": opening,
        "closing": closing,
        "rows": [asdict(r) for r in rows],
        "totals": {
            "count": len(rows),
            "debit": round(sum(r.debit or 0 for r in rows), 2),
            "credit": round(sum(r.credit or 0 for r in rows), 2),
            "unreconciled": sum(1 for r in rows if not r.reconciled),
        },
    }
