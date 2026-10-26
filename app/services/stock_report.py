"""The stock report (STOCK REPORT-PLEDGE) sent back on WhatsApp once a
delivery order (or an L/C letter bringing goods in) is confirmed: one of
the company's own report files (app/stock_report_templates/) filled in
completely — each line's description, opening balance, quantity received /
delivered, balance on hand, rate, value, margin and DP value — plus the
date, the bank's city, the godown / customer and the document references.

The sample is filled through LibreOffice (app/services/stock_report_uno.py)
so that everything already in it stays as it is: its TOTAL / drawing-power
formulas, formatting and letterhead. Each line gets formulas too (balance
= opening + received − delivered, value = balance × rate, margin, DP), so
the sheet recalculates when a figure is changed. A rate comes from the
enclosed invoice; a delivery order has none, so its value cells show 0
until one is typed in. LibreOffice also makes the PDF.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import settings
from app.models import InvoiceRecord
from app.services import stock_templates
from app.services.stock_templates import Template

logger = logging.getLogger(__name__)

PKT = timezone(timedelta(hours=5))  # Pakistan has no DST

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PDF_MIME = "application/pdf"

_UNO_SCRIPT = Path(__file__).resolve().with_name("stock_report_uno.py")
_office_lock = threading.Lock()  # one LibreOffice at a time: they'd share its profile

_QUANTITY = re.compile(r"^\s*([\d,]*\.?\d+)\s*([A-Za-z.]*)\s*$")
_ZERO_WORDS = {"nil", "-", "—", "none"}
_LC = re.compile(r"\bl\s*/?\s*c\b", re.IGNORECASE)


class StockReportError(Exception):
    pass


@dataclass
class StockReport:
    xlsx_path: str
    pdf_path: str
    customer: str
    template: str


def delivery_order(record: InvoiceRecord) -> dict | None:
    """The delivery-order fields of a confirmed document, or None if it isn't one."""
    order = json.loads(record.raw_extracted_json or "{}").get("delivery_order")
    return order if order and order.get("rows") else None


def _quantity(value: str | None) -> float | None:
    """'10.710 MT' → 10.71; 'NIL' → 0; None if it isn't a plain quantity."""
    value = (value or "").strip()
    if value.lower() in _ZERO_WORDS:
        return 0.0
    m = _QUANTITY.match(value)
    return float(m[1].replace(",", "")) if m else None


_KILOS = {"kg", "kgs", "kilo", "kilos", "kilogram", "kilograms"}


def _unit(value: str | None) -> str:
    """'5600 KGS' → 'kgs'; '' when there is no unit."""
    m = _QUANTITY.match((value or "").strip())
    return m[2].lower().rstrip(".") if m else ""


def _import_conversion(order: dict) -> bool:
    """An invoice in USD is shown in rupees per metric ton (like the approved reports) when a rate is set."""
    return (order.get("currency") or "").strip().upper() == "USD" and settings.usd_pkr_rate > 0


def _customer(order: dict) -> str:
    name = (order.get("customer_name") or "").strip()
    return re.sub(r"^m/s\.?\s*", "", name, flags=re.IGNORECASE) or "Customer"


def _safe_filename(text: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text).strip(" .")[:80] or "Stock Report"


def pick_template(record: InvoiceRecord, order: dict) -> Template:
    """The customer's own format if there is one, else the bank's, else the default."""
    entries = stock_templates.load()
    customer = stock_templates.normalise(_customer(order))
    for entry in entries:
        if any(c in customer for c in entry.template.customers):
            return entry.template
    extra = json.loads(record.raw_extracted_json or "{}")
    bank = f"{record.bank_code} {record.bank_name} {extra.get('bank_name')}".lower()
    for entry in entries:
        if any(b in bank for b in entry.template.banks):
            return entry.template
    entry = stock_templates.default(entries)
    if entry is None:
        raise StockReportError("There are no report formats. Add one in the dashboard's Report formats tab.")
    return entry.template


def _fill_in(text: str, **values: str) -> str:
    """Fills {name} placeholders — and nothing else, so a sample's own braces survive."""
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


def _shifted(cell: str, after_row: int, count: int) -> str:
    """A cell below the stock lines, moved down by the rows inserted into them."""
    m = re.match(r"([A-Z]+)(\d+)$", cell)
    return f"{m[1]}{int(m[2]) + count}" if m and int(m[2]) > after_row else cell


def build_job(template: Template, order: dict, dated: datetime) -> dict:
    """What stock_report_uno.py writes into the sample. Extra stock lines
    are inserted below the sample's last line, and its TOTAL row's SUM
    ranges are stretched over them, so the totals take them in. Formulas
    the sample already has are kept (see stock_report_uno.py)."""
    rows = order["rows"]
    convert = _import_conversion(order)
    slots = template.last_row - template.first_row + 1
    job: dict = {"values": [], "merges": [], "text_style": [], "date_cells": [template.date_cell] if template.date_cell else [], "date": f"{dated:%d-%m-%Y}"}
    if len(rows) > slots:
        added = len(rows) - slots
        job.update(
            insert_after=template.last_row, insert_count=added, first_row=template.first_row, last_row=template.last_row
        )
        job["merges"] = [
            f"{a}{r}:{b}{r}"
            for r in range(template.last_row + 1, template.last_row + added + 1)
            for a, b in template.row_merges
        ]
    if template.clear_sample:
        mapped = [
            c for c in (
                template.description, template.opening, template.received, template.delivered, template.balance,
                template.rate, template.value, template.dp, template.margin, template.less_margin, template.lc_column,
                template.last_column,
            ) if c
        ]
        job["clear"] = {
            "first_row": template.first_row, "last_row": max(template.last_row, template.first_row + len(rows) - 1),
            "first_column": min(mapped, key=lambda c: (len(c), c)), "last_column": max(mapped, key=lambda c: (len(c), c)),
        }

    t = template
    values = job["values"]

    def put(cell: str, value, format_from: str | None = None) -> None:
        values.append([cell, value, format_from] if format_from else [cell, value])

    for i, item in enumerate(rows):
        r = t.first_row + i
        description = item.get("description") or ""
        column = t.lc_column if t.lc_column and _LC.search(description) else t.description
        put(f"{column}{r}", description)
        job["text_style"].append([f"{column}{r}", f"{column}{t.first_row}"])  # the sample's own stock-line look

        # Quantities: as stated; a movement not stated is 0, and a balance
        # not stated (goods coming in on an L/C) is worked out in the sheet.
        # A foreign invoice counted in kilos is reported in metric tons (the rate then follows: per ton).
        per = 1.0
        if convert and any(_unit(item.get(k)) in _KILOS for k in ("opening_balance", "quantity_received", "quantity_released", "balance_after")):
            per = 1000.0
        for key, col in (("opening_balance", t.opening), ("quantity_received", t.received), ("quantity_released", t.delivered)):
            if not col:
                continue
            value = (item.get(key) or "").strip()
            number = _quantity(value) if value else 0.0
            put(f"{col}{r}", value if number is None else number / per, f"{col}{t.first_row}")
        balance = (item.get("balance_after") or "").strip()
        if balance and _quantity(balance) is not None:
            put(f"{t.balance}{r}", _quantity(balance) / per, f"{t.balance}{t.first_row}")
        else:
            terms = "".join(
                f"{sign}{col}{r}" for sign, col in (("+", t.opening), ("+", t.received), ("-", t.delivered)) if col
            ).lstrip("+")
            put(f"{t.balance}{r}", f"={terms}", f"{t.balance}{t.first_row}")

        # Value = balance on hand × rate; then margin and DP value.
        rate = _quantity(item.get("rate_per_unit"))
        if rate is not None:
            if convert:
                rate = round(rate * per * settings.usd_pkr_rate, 2)
            put(f"{t.rate}{r}", rate, f"{t.rate}{t.first_row}")
        divisor = f"/{t.value_divisor}" if t.value_divisor != 1 else ""
        put(f"{t.value}{r}", f"={t.balance}{r}*{t.rate}{r}{divisor}", f"{t.value}{t.first_row}")
        if t.margin and t.margin_pct is not None:
            put(f"{t.margin}{r}", t.margin_pct)
            put(f"{t.less_margin}{r}", f"={t.value}{r}*{t.margin}{r}/100", f"{t.less_margin}{t.first_row}")
            put(f"{t.dp}{r}", f"={t.value}{r}-{t.less_margin}{r}", f"{t.dp}{t.first_row}")
        elif t.dp and t.margin_pct is not None:
            put(f"{t.dp}{r}", f"={t.value}{r}*{100 - t.margin_pct}/100", f"{t.dp}{t.first_row}")

    # Only the bank-approved sentence goes in — nothing is appended to it — and it keeps the sample's look.
    location = order.get("pledge_location") or "—"
    put(t.text_cell, _fill_in(t.text, location=location, customer=_customer(order)))
    job["rich"] = {"cell": t.text_cell, "location": location, "customer": _customer(order)}

    branch = (order.get("bank_branch") or "").strip()
    if t.city_cell and "," in branch:
        put(t.city_cell, _fill_in(t.city_text, city=branch.rsplit(",", 1)[1].strip().rstrip(".")))

    currency = (order.get("currency") or "").strip().upper()
    if currency and currency not in ("PKR", "RS", "RS.") and not convert:
        job["replacements"] = [
            [_shifted(cell, t.last_row, max(len(rows) - slots, 0)), r"\bRs\b\.?", currency] for cell in t.currency_cells
        ]
    return job


def _soffice() -> str | None:
    if settings.libreoffice_path:
        return settings.libreoffice_path
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    for path in (r"C:\Program Files\LibreOffice\program\soffice.exe", r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"):
        if os.path.exists(path):
            return path
    return None


def _office_python(soffice: str) -> str:
    """LibreOffice's own Python on Windows (it ships with `uno`); elsewhere
    the system python3, with the python3-uno package."""
    bundled = Path(soffice).with_name("python.exe" if os.name == "nt" else "python")
    if bundled.exists():
        return str(bundled)
    # The apt python3-uno package only works with the distro python, not e.g. /usr/local/bin/python3 in a python image.
    return "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else "python3"


def render(template: Template, order: dict, key: str, dated: datetime | None = None) -> StockReport:
    """Fills `template` with the order's lines → an .xlsx and a .pdf under stock_report_dir/<key>/."""
    soffice = _soffice()
    if not soffice:
        raise StockReportError("LibreOffice is not installed on the server (needed to fill the stock report)")

    path = stock_templates.templates_dir() / template.file
    if not path.exists():
        raise StockReportError(f"The report format file {template.file} is missing from {path.parent}")
    dated = dated or datetime.now(PKT)
    customer = _customer(order)
    out_dir = os.path.abspath(os.path.join(settings.stock_report_dir, key))
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.join(out_dir, f"{_safe_filename(customer)} {dated:%d-%m-%Y}")

    job = build_job(template, order, dated)
    job.update(
        soffice=soffice,
        # A profile of its own, so it doesn't collide with a LibreOffice window the user has open.
        profile_url=Path(settings.stock_report_dir, ".libreoffice_profile").resolve().as_uri(),
        template=str(path),
        xlsx_out=base + ".xlsx",
        pdf_out=base + ".pdf",
    )
    job_path = os.path.join(out_dir, f"job-{os.getpid()}-{threading.get_ident()}.json")
    with open(job_path, "w", encoding="utf-8") as f:
        json.dump(job, f)

    # LibreOffice's Python must not pick up this app's venv.
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
    try:
        with _office_lock:
            result = subprocess.run(
                [_office_python(soffice), str(_UNO_SCRIPT), job_path], capture_output=True, text=True, timeout=300, env=env
            )
    except (OSError, subprocess.SubprocessError) as exc:
        raise StockReportError(f"LibreOffice couldn't be run: {exc}") from exc
    finally:
        os.remove(job_path)
    if result.returncode != 0 or not os.path.exists(job["xlsx_out"]) or not os.path.exists(job["pdf_out"]):
        logger.error("Stock report %s failed: %s", key, (result.stderr or result.stdout)[-2000:])
        raise StockReportError("LibreOffice failed while filling the report (see the server log)")

    logger.info("Stock report %s filled from %s", key, template.file)
    return StockReport(xlsx_path=job["xlsx_out"], pdf_path=job["pdf_out"], customer=customer, template=template.file)


def latest(record: InvoiceRecord) -> StockReport | None:
    """The stock report already made for this record (the newest), or None."""
    out_dir = Path(settings.stock_report_dir, str(record.id))
    pdfs = sorted(out_dir.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True) if out_dir.is_dir() else []
    if not pdfs:
        return None
    xlsx = pdfs[0].with_suffix(".xlsx")
    order = delivery_order(record)
    return StockReport(
        xlsx_path=str(xlsx) if xlsx.exists() else "", pdf_path=str(pdfs[0]),
        customer=_customer(order) if order else pdfs[0].stem, template="",
    )


def build(record: InvoiceRecord) -> StockReport:
    order = delivery_order(record)
    if order is None:
        raise StockReportError(f"#{record.id} has no delivery-order details to report")
    return render(pick_template(record, order), order, str(record.id))
