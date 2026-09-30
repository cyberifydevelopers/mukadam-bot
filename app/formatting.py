"""Plain-text rendering of extracted documents, shared by the WhatsApp
prompt, the "Saved" acknowledgement and the Sheet row — so all three show
the same amount and items."""

import json
import re

from app.models import InvoiceRecord

_SERIAL_COLUMN = re.compile(r"^(sr|s\.?\s*no|no|#|serial|sl)\.?\s*#?$", re.IGNORECASE)
_NUMBER = re.compile(r"^\s*([\d,]*\.?\d+)\s*([A-Za-z.]*)\s*$")
_ZERO_WORDS = {"nil", "-", "—", "none", "0"}
# Which column of an items table holds the quantity being moved/paid, in
# order of preference.
_QUANTITY_COLUMNS = ("released", "deliver", "quantity", "qty", "stock", "amount", "total")
MAX_ITEM_ROWS = 25


def format_money(amount: str | None, currency: str | None) -> str | None:
    if not amount:
        return None
    try:
        shown = f"{float(amount.replace(',', '')):,.2f}"
    except ValueError:
        shown = amount
    return f"{currency or 'PKR'} {shown}"


def quantity_total(table: dict | None) -> tuple[str, str] | None:
    """(column name, total with unit) for a document that lists quantities
    instead of a money amount — e.g. a delivery order's "Quantity to be
    Released": 0.420 + 3 + 7 + 7.35 MT → "17.770 MT". None when there's no
    such column or its values don't add up cleanly (mixed units, text)."""
    if not table or not table.get("rows"):
        return None
    columns = [c or "" for c in table.get("columns") or []]
    for key in _QUANTITY_COLUMNS:
        idx = next((i for i, c in enumerate(columns) if key in c.lower()), None)
        if idx is None:
            continue
        total, units, decimals = 0.0, set(), 0
        for row in table["rows"]:
            value = (row[idx] if idx < len(row) else "").strip()
            if value.lower() in _ZERO_WORDS or not value:
                continue
            m = _NUMBER.match(value)
            if not m:
                return None
            number = m[1].replace(",", "")
            decimals = max(decimals, len(number.split(".")[1]) if "." in number else 0)
            total += float(number)
            if m[2]:
                units.add(m[2].upper())
        if len(units) > 1:
            return None
        unit = f" {units.pop()}" if units else ""
        return columns[idx], f"{total:,.{decimals}f}{unit}"
    return None


def amount_line(record: InvoiceRecord) -> tuple[str, str] | None:
    """("Amount", "PKR 245,500.00") when the document states a money amount,
    else ("Total <quantity column>", "17.770 MT") from its items, else None."""
    extra = json.loads(record.raw_extracted_json or "{}")
    money = format_money(record.amount, extra.get("currency"))
    if money:
        return "Amount", money
    qty = quantity_total(extra.get("items_table"))
    if qty:
        return f"Total {qty[0].lower()}", qty[1]
    return None


def format_items(columns: list[str], rows: list[list[str]]) -> list[str]:
    """A document's table as phone-friendly plain text: a value shared by
    every row is shown once at the top; then each row is numbered, with its
    first column as the title and every other column on its own indented
    line (a fixed-width table wraps unreadably on a phone at 5-6 columns),
    and a blank line between rows."""
    if not rows:
        return []
    keep = [i for i, c in enumerate(columns) if not _SERIAL_COLUMN.match((c or "").strip())] or list(range(len(columns)))
    lines: list[str] = []
    if len(rows) > 1:
        for i in list(keep[1:]):
            values = {(r[i] if i < len(r) else "").strip() for r in rows}
            if len(values) == 1 and (value := values.pop()) and i < len(columns):
                lines.append(f"{columns[i]}: {value}")
                keep.remove(i)
        if lines:
            lines.append("")
    for n, row in enumerate(rows[:MAX_ITEM_ROWS], start=1):
        cells = [(columns[i] if i < len(columns) else "", row[i] if i < len(row) else "") for i in keep]
        cells = [(c, (v or "").strip()) for c, v in cells if (v or "").strip()]
        if not cells:
            continue
        lines.append(f"{n}. {cells[0][1]}")
        lines += [f"     {c}: {v}" if c else f"     {v}" for c, v in cells[1:]]
        lines.append("")
    if len(rows) > MAX_ITEM_ROWS:
        lines.append(f"...and {len(rows) - MAX_ITEM_ROWS} more (see the dashboard)")
    while lines and not lines[-1]:
        lines.pop()
    return lines
