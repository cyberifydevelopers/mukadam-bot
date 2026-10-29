"""The AI's read of a new stock-report format (an Excel sample): which rows
are the stock lines, which column holds what, and where the date and the
godown / customer sentence are — written down as a `Template`
(stock_templates.py) so the bot can fill invoices into that sample.

The model is shown every non-empty cell of the sample (address: value, with
formulas as written) and the merged ranges, and answers through one forced
tool call. What can be worked out from the sheet itself is then worked out
again here and wins over the model: where the TOTAL row is (which ends the
stock lines), which merged cells the lines use, the header's right edge. Every
cell the model names is checked against the sheet, so a wrong guess is
refused with a reason rather than filling the wrong cell later.
"""

import json
import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import openpyxl
from openai import APIError
from openpyxl.utils import get_column_letter

from app.config import settings
from app.parsers.llm_parser import LlmParseError, _api_error, chat_with_fallback, openrouter_client
from app.services import stock_report, stock_templates
from app.services.stock_templates import Template

logger = logging.getLogger(__name__)

_MAX_ROWS = 80
_COLUMN = re.compile(r"^[A-Z]{1,2}$")
_CELL = re.compile(r"^([A-Z]{1,2})([1-9]\d{0,3})$")


class ReviewError(Exception):
    """The sample can't be used as it is (the message says why, and is shown to the user)."""


@dataclass
class Review:
    name: str
    template: Template
    notes: list[str] = field(default_factory=list)


_COLUMN_FIELD = {"type": ["string", "null"], "description": "Column letter only, e.g. 'G' (no row number); null if the sample has no such column"}
_FUNCTION = {
    "name": "describe_stock_report_layout",
    "description": (
        "Describe the layout of this stock-report sample (a bank 'stock report / pledge' form that a borrower "
        "fills in for the bank: one line per kind of goods, then a TOTAL row, then a summary block). Name only "
        "cells that really exist in the dump below; never guess a cell."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "customer_name": {"type": ["string", "null"], "description": "The customer (borrower) this sample is for, from the file name or the text, e.g. 'Al Mustafa Rice Mills'"},
            "customer_keywords": {
                "type": "array", "items": {"type": "string"},
                "description": (
                    "Short lower-case name fragments that identify this customer in a delivery order or L/C letter, "
                    "letters and spaces only, e.g. ['al mustafa', 'mustafa rice']. Distinctive words only — never 'industries', "
                    "'limited', 'pvt', 'ltd', 'mills' alone"
                ),
            },
            "bank_name": {"type": ["string", "null"], "description": "The bank the form is addressed to, e.g. 'Allied Bank Limited'"},
            "header_row": {"type": "integer", "description": "Row number of the table's column headings (Description of Goods, Opening Balance, …)"},
            "first_row": {"type": "integer", "description": "Row number of the first stock line, just under the headings"},
            "last_row": {"type": "integer", "description": "Row number of the last stock line (the row above TOTAL)"},
            "description": {**_COLUMN_FIELD, "description": "Column of the goods description (the leftmost cell of a merged block)"},
            "opening": {**_COLUMN_FIELD, "description": "Opening balance / balance as per last stock report"},
            "received": {**_COLUMN_FIELD, "description": "Quantity received"},
            "delivered": {**_COLUMN_FIELD, "description": "Quantity delivered / released"},
            "balance": {**_COLUMN_FIELD, "description": "Balance on hand"},
            "rate": {**_COLUMN_FIELD, "description": "Rate per unit"},
            "value": {**_COLUMN_FIELD, "description": "Total / approximate value of the line (balance × rate)"},
            "margin": {**_COLUMN_FIELD, "description": "A per-line margin % column, if there is one"},
            "less_margin": {**_COLUMN_FIELD, "description": "A per-line 'less margin' amount column, if there is one"},
            "dp": {**_COLUMN_FIELD, "description": "Per-line DP / drawing-power value column, if there is one"},
            "margin_pct": {"type": ["number", "null"], "description": "The margin this sample uses, as a whole number (30 for 30%); null if none"},
            "value_divisor": {"type": "integer", "enum": [1, 1000, 1000000], "description": "1 normally; 1000000 if values are in millions"},
            "date_cell": {"type": ["string", "null"], "description": "Cell holding 'Dated: dd-mm-yyyy' (or just 'Dated')"},
            "text_cell": {"type": "string", "description": "Cell (top-left of the merged block) with the sentence saying the stock is lying in a godown, on the account of the customer"},
            "text_template": {
                "type": "string",
                "description": (
                    "That sentence rewritten as a template for any customer: keep its wording but put {location} where "
                    "the godown / address goes and {customer} where the customer's name goes (leave {customer} out if the "
                    "sentence doesn't name the customer). Everything else stays word for word as written, to the very end. Must contain {location}"
                ),
            },
            "city_cell": {"type": ["string", "null"], "description": "Cell with the bank branch / city line under the bank's name (e.g. 'Lahore.' or 'Branch DEH UDHI THUL .'); null if none"},
            "city_text": {"type": ["string", "null"], "description": "That line as a template with {city}, e.g. '{city}.' or 'Branch {city}.'"},
            "currency_cells": {
                "type": "array", "items": {"type": "string"},
                "description": (
                    "Label cells in the summary block (below TOTAL) whose 'Rs' is the currency of the STOCK's value: Total Value, "
                    "Drawing Power, Less margin. NOT outstanding / shortfall / insured / D-O labels, which stay in rupees"
                ),
            },
            "notes": {
                "type": "array", "items": {"type": "string"},
                "description": (
                    "Short plain-language remarks for the user about anything unusual: formulas with fixed numbers in them "
                    "(e.g. '=G16/0.325'), columns that can't be filled from an invoice (units, drums, weights), several "
                    "tables, a layout that doesn't look like a stock report"
                ),
            },
        },
        "required": [
            "header_row", "first_row", "last_row", "description", "opening", "received", "delivered", "balance", "rate",
            "value", "text_cell", "text_template", "customer_keywords", "notes",
        ],
    },
}
_TOOL_CHOICE = {"type": "function", "function": {"name": "describe_stock_report_layout"}}


def _to_xlsx(path: Path, workdir: Path) -> Path:
    """The sample as .xlsx for reading (an old .xls is converted by LibreOffice; the original is what gets filled)."""
    if path.suffix.lower() == ".xlsx":
        return path
    soffice = stock_report._soffice()
    if not soffice:
        raise ReviewError("LibreOffice isn't installed, so an old .xls file can't be read. Save it as .xlsx and add it again.")
    try:
        subprocess.run(
            [soffice, f"-env:UserInstallation={(workdir / 'profile').as_uri()}", "--headless", "--convert-to", "xlsx",
             "--outdir", str(workdir), str(path)],
            capture_output=True, timeout=180, check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReviewError(f"The .xls file couldn't be opened: {exc}") from exc
    converted = workdir / (path.stem + ".xlsx")
    if not converted.exists():
        raise ReviewError("The .xls file couldn't be converted for reading. Save it as .xlsx and add it again.")
    return converted


def _text(cell) -> str:
    return "" if cell.value is None else str(cell.value)


def _dump(ws) -> str:
    lines = [f"Sheet '{ws.title}', used range {ws.dimensions}"]
    for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, _MAX_ROWS)):
        cells = [f"{c.coordinate}: {_text(c).strip()[:700]!r}" for c in row if _text(c).strip()]
        if cells:
            lines.append(" | ".join(cells))
    merged = ", ".join(str(m) for m in ws.merged_cells.ranges if m.min_row <= _MAX_ROWS)
    lines.append(f"Merged ranges: {merged or 'none'}")
    return "\n".join(lines)


def _find_header_row(ws) -> int | None:
    for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 40)):
        if any("description" in _text(c).lower() for c in row):
            return row[0].row
    return None


def _find_total_row(ws, after: int) -> int | None:
    for row in ws.iter_rows(min_row=after + 1, max_row=min(ws.max_row, _MAX_ROWS)):
        if any(_text(c).strip().lower().startswith("total") for c in row):
            return row[0].row
    return None


def _column(value, name: str, *, required: bool) -> str | None:
    if value is None or value == "":
        if required:
            raise ReviewError(f"The AI couldn't find the '{name}' column in this sample.")
        return None
    value = re.sub(r"\d+$", "", str(value).strip().upper())  # 'A14' → 'A': a cell instead of a column is still clear
    if not _COLUMN.match(value):
        raise ReviewError(f"The AI named '{value}' as the '{name}' column, which isn't a column letter.")
    return value


def _cell(ws, ref, label: str):
    """The sheet's cell for `ref`, or None if the AI named something that isn't there."""
    m = _CELL.match(str(ref or "").strip().upper())
    if not m or int(m[2]) > max(ws.max_row, 1) or _column_index(m[1]) > max(ws.max_column, 1):
        return None
    return ws[m[0]]


def _column_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


def _llm(dump: str, original_name: str, customer_hint: str) -> dict:
    prompt = (
        "A bank requires a borrower to send a 'stock report' (pledge / muccadam report) in an Excel form. Below is "
        f"one such sample, file name {original_name!r}"
        + (f", for the customer {customer_hint!r}" if customer_hint else "")
        + ". It may already contain an old report's figures. Work out the layout so software can later write a "
        "different customer's goods into it: the stock lines are the rows between the column headings and the TOTAL row.\n\n"
        + dump
    )
    try:
        response = chat_with_fallback(
            openrouter_client(),
            temperature=0,
            tools=[{"type": "function", "function": _FUNCTION}],
            tool_choice=_TOOL_CHOICE,
            messages=[{"role": "user", "content": prompt}],
        )
    except APIError as exc:
        raise ReviewError(str(_api_error(exc))) from exc
    except LlmParseError as exc:
        raise ReviewError(str(exc)) from exc
    calls = response.choices[0].message.tool_calls
    if not calls:
        raise ReviewError("The AI didn't answer. Please try again.")
    try:
        return json.loads(calls[0].function.arguments)
    except ValueError as exc:
        raise ReviewError("The AI's answer was cut short. Please try again.") from exc


def review(path: Path, original_name: str, customer_hint: str = "") -> Review:
    """Reads the sample at `path` and returns its layout; raises ReviewError if it can't be used."""
    if path.suffix.lower() not in stock_templates.EXTENSIONS:
        raise ReviewError("Only Excel files (.xlsx or .xls) can be used as a report format.")
    with tempfile.TemporaryDirectory() as tmp:
        try:
            workbook = openpyxl.load_workbook(_to_xlsx(path, Path(tmp)))
        except ReviewError:
            raise
        except Exception as exc:  # noqa: BLE001 — a corrupt or password-protected workbook
            raise ReviewError(f"That Excel file couldn't be opened ({exc.__class__.__name__}).") from exc
        ws = workbook.worksheets[0]
        dump = _dump(ws)
        if not dump.count("\n") > 3:
            raise ReviewError("That Excel file's first sheet is empty.")
        answer = _llm(dump, original_name, customer_hint)
        return _validate(ws, answer, original_name, customer_hint)


def _validate(ws, a: dict, original_name: str, customer_hint: str) -> Review:
    notes = [str(n).strip() for n in a.get("notes") or [] if str(n).strip()]

    header = _find_header_row(ws) or a.get("header_row")
    if not isinstance(header, int) or not 1 <= header < ws.max_row:
        raise ReviewError("The AI couldn't find the table's column headings.")
    first = a.get("first_row") if isinstance(a.get("first_row"), int) and a["first_row"] > header else header + 1
    total = _find_total_row(ws, first - 1)
    if total is not None and total > first:
        last = total - 1  # the TOTAL row ends the stock lines
    elif isinstance(a.get("last_row"), int) and a["last_row"] >= first:
        last = a["last_row"]
        notes.append("No TOTAL row was found under the stock lines, so the AI's last line was used.")
    else:
        raise ReviewError("The AI couldn't tell where the stock lines end.")

    columns = {
        "description": _column(a.get("description"), "description", required=True),
        "opening": _column(a.get("opening"), "opening balance", required=False),
        "received": _column(a.get("received"), "quantity received", required=False),
        "delivered": _column(a.get("delivered"), "quantity delivered", required=False),
        "balance": _column(a.get("balance"), "balance on hand", required=True),
        "rate": _column(a.get("rate"), "rate", required=True),
        "value": _column(a.get("value"), "value", required=True),
    }
    if not (columns["opening"] or columns["received"]):
        raise ReviewError("The AI couldn't find the opening balance or quantity received columns.")
    margin, less_margin, dp = (_column(a.get(k), k, required=False) for k in ("margin", "less_margin", "dp"))
    if not (margin and less_margin and dp):
        if margin or less_margin:
            notes.append("The margin columns are incomplete, so the sample's own margin formulas are left as they are.")
        margin = less_margin = None
    pct = a.get("margin_pct")
    pct = float(pct) if isinstance(pct, (int, float)) and 0 <= pct < 100 else None
    if dp and pct is None:
        notes.append("No margin % was found, so DP value is only filled where the sample has no formula of its own.")
    divisor = a.get("value_divisor") if a.get("value_divisor") in (1000, 1000000) else 1

    # Edges and merges, read from the sheet.
    header_cells = [c.column for c in ws[header] if _text(c).strip()]
    last_column = get_column_letter(max(header_cells)) if header_cells else None
    row_merges = []
    for m in ws.merged_cells.ranges:
        if m.min_row == m.max_row == first and m.min_col != m.max_col:
            row_merges.append((get_column_letter(m.min_col), get_column_letter(m.max_col)))

    text_cell = _cell(ws, a.get("text_cell"), "text")
    if text_cell is None or len(_text(text_cell).strip()) < 15:
        text_cell = next(
            (c for row in ws.iter_rows(max_row=min(ws.max_row, header))
             for c in row if re.search(r"stock", _text(c), re.I) and re.search(r"godown|situated|lying", _text(c), re.I)),
            None,
        )
    if text_cell is None:
        raise ReviewError("The AI couldn't find the sentence saying where the stock is lying.")
    template_text = re.sub(r"(?<=\w)\{location\}", " {location}", str(a.get("text_template") or "").strip())
    if "{location}" not in template_text:
        template_text = (_text(text_cell).strip().rstrip(".") + " {location}.").strip()
        notes.append("The godown sentence has no clear place for the address, so it is added at its end.")

    date_cell = _cell(ws, a.get("date_cell"), "date")
    if date_cell is None or not re.search(r"dat", _text(date_cell), re.I):
        date_cell = next(
            (c for row in ws.iter_rows(max_row=min(ws.max_row, 14)) for c in row if re.search(r"dated", _text(c), re.I)),
            None,
        )
    if date_cell is None:
        notes.append("No 'Dated:' cell was found, so the date is left as it is in the sample.")

    city_cell = _cell(ws, a.get("city_cell"), "city")
    city_text = str(a.get("city_text") or "")
    if city_cell is None or "{city}" not in city_text or not _text(city_cell).strip():
        city_cell, city_text = None, "{city}."

    currency: list[str] = []
    for ref in a.get("currency_cells") or []:
        c = _cell(ws, ref, "currency")
        if c is not None and not re.search(r"\bRs\b", _text(c)):  # the label 'Total Value :' with 'Rs:' in the next cell
            c = next((x for x in ws[c.row] if x.column > c.column and re.search(r"\bRs\b", _text(x))), None)
        if c is not None and re.search(r"\bRs\b", _text(c)) and c.coordinate not in currency:
            currency.append(c.coordinate)

    customers = stock_templates.clean_words([*(a.get("customer_keywords") or []), customer_hint])
    stem = re.sub(r"\s*\d{1,2}[-.]\d{1,2}[-.]\d{2,4}\s*$", "", Path(original_name).stem).strip()
    name = (customer_hint or a.get("customer_name") or stem or "New format").strip()
    if not customers:
        customers = stock_templates.clean_words([name])
        notes.append("No customer name could be told apart, so the format is matched on its name only. Edit the match words if needed.")
    bank = (a.get("bank_name") or "").strip()
    if bank:
        notes.append(f"Addressed to {bank}: this format is also used for that bank's other customers (change it under 'Also use for this bank').")

    template = Template(
        file="", customers=customers, banks=stock_templates.bank_words(bank), first_row=first, last_row=last, description=columns["description"],
        opening=columns["opening"], received=columns["received"], delivered=columns["delivered"],
        balance=columns["balance"], rate=columns["rate"], value=columns["value"], dp=dp,
        date_cell=date_cell.coordinate if date_cell is not None else None, text_cell=text_cell.coordinate,
        text=template_text, margin=margin, less_margin=less_margin, margin_pct=pct, value_divisor=divisor,
        currency_cells=tuple(currency), city_cell=city_cell.coordinate if city_cell is not None else None, city_text=city_text,
        row_merges=tuple(row_merges), clear_sample=True, last_column=last_column,
    )
    return Review(name=name, template=template, notes=notes)
