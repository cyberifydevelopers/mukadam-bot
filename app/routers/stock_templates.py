"""JSON API behind the dashboard's "Report formats" tab: the Excel samples
the stock report is filled into (app/services/stock_templates.py). A sample
that is added is read by the AI (stock_template_review.py) to learn where
each figure goes; its own formulas are never touched.

Like the rest of /api this has no login — it is meant for 127.0.0.1 only.
"""

import logging
import tempfile
from dataclasses import replace
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.services import stock_report, stock_template_review, stock_templates
from app.services.stock_report import StockReportError
from app.services.stock_template_review import ReviewError
from app.services.stock_templates import Entry, TemplateError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/stock-templates", tags=["stock-templates"])

_MAX_BYTES = 10 * 1024 * 1024

# What the preview is filled with, so a format can be checked before a real invoice goes into it.
_PREVIEW_ORDER = {
    "customer_name": "Preview Customer",
    "pledge_location": "Sample godown, Sample City",
    "bank_branch": "Main Branch, Sample City",
    "currency": "PKR",
    "rows": [
        {"description": "Sample goods A", "opening_balance": "100", "quantity_received": "50",
         "quantity_released": "20", "rate_per_unit": "1000"},
        {"description": "Sample goods B", "opening_balance": "40", "quantity_received": "10",
         "quantity_released": "5", "rate_per_unit": "2500"},
    ],
}


def _json(entry: Entry) -> dict:
    t = entry.template
    return {
        **entry.to_json(),
        "file_exists": entry.path.exists(),
        "customers": list(t.customers),
        "banks": list(t.banks),
        "summary": {
            "stock_rows": f"{t.first_row}–{t.last_row}" if t.last_row != t.first_row else str(t.first_row),
            "columns": {k: v for k, v in (
                ("Description", t.description), ("Opening", t.opening), ("Received", t.received),
                ("Delivered", t.delivered), ("Balance", t.balance), ("Rate", t.rate), ("Value", t.value),
                ("Margin %", t.margin), ("Less margin", t.less_margin), ("DP", t.dp),
            ) if v},
            "date_cell": t.date_cell,
            "text_cell": t.text_cell,
            "margin_pct": t.margin_pct,
        },
    }


def _fail(exc: Exception, status: int = 422) -> HTTPException:
    return HTTPException(status, str(exc))


def _words(value) -> list[str]:
    """'al mustafa, mustafa rice' (or a list) → separate words."""
    if isinstance(value, str):
        value = value.replace("\n", ",").split(",")
    return [w for w in (value or []) if str(w).strip()]


@router.get("")
def list_templates():
    try:
        return {"templates": [_json(e) for e in stock_templates.load()]}
    except TemplateError as exc:
        raise _fail(exc, 500) from exc


@router.post("")
def add_template(file: UploadFile, customer: str = Form(""), bank: str = Form("")):
    """Adds an Excel sample: the AI reads its layout, then it appears in the list."""
    name = Path(file.filename or "").name
    if Path(name).suffix.lower() not in stock_templates.EXTENSIONS:
        raise HTTPException(422, "Please choose an Excel file (.xlsx or .xls).")
    data = file.file.read(_MAX_BYTES + 1)
    if not data or len(data) > _MAX_BYTES:
        raise HTTPException(422, "That file is empty or larger than 10 MB.")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / name
        path.write_bytes(data)
        try:
            review = stock_template_review.review(path, name, customer.strip())
        except ReviewError as exc:
            raise _fail(exc) from exc
    template = replace(review.template, banks=stock_templates.clean_words(_words(bank)))
    try:
        entry = stock_templates.add(review.name, name, data, template, review.notes)
    except TemplateError as exc:
        raise _fail(exc, 500) from exc
    return _json(entry)


class MatchIn(BaseModel):
    name: str | None = None
    customers: str | list[str] | None = None
    banks: str | list[str] | None = None


@router.put("/{ident}")
def update_template(ident: str, body: MatchIn):
    """Renames a format and edits the customers / bank it is used for."""
    try:
        entry = stock_templates.get(ident)
        customers = stock_templates.clean_words(_words(body.customers)) if body.customers is not None else None
        if customers is not None and not customers:
            raise HTTPException(422, "A format needs at least one customer name (3 letters or more) to be matched on.")
        banks = stock_templates.clean_words(_words(body.banks)) if body.banks is not None else None
        return _json(stock_templates.update(entry.id, name=body.name, customers=customers, banks=banks))
    except TemplateError as exc:
        raise _fail(exc, 404) from exc


@router.post("/{ident}/review")
def review_again(ident: str):
    """Has the AI read the uploaded sample again (the customer / bank match words stay as they are)."""
    try:
        entry = stock_templates.get(ident)
        if entry.source == "builtin":
            raise HTTPException(422, "The built-in samples were set up by hand and don't need a review.")
        review = stock_template_review.review(entry.path, entry.original_name)
    except TemplateError as exc:
        raise _fail(exc, 404) from exc
    except ReviewError as exc:
        raise _fail(exc) from exc
    layout = replace(
        review.template, file=entry.template.file, customers=entry.template.customers, banks=entry.template.banks
    )
    return _json(stock_templates.update(entry.id, template=layout, notes=review.notes))


@router.delete("/{ident}")
def delete_template(ident: str):
    try:
        stock_templates.remove(ident)
    except TemplateError as exc:
        raise _fail(exc, 404) from exc
    return {"ok": True}


@router.get("/{ident}/file")
def download_template(ident: str):
    try:
        entry = stock_templates.get(ident)
    except TemplateError as exc:
        raise _fail(exc, 404) from exc
    if not entry.path.exists():
        raise HTTPException(404, "The file is missing from the server.")
    return FileResponse(entry.path, filename=entry.original_name)


@router.get("/{ident}/preview")
def preview_template(ident: str):
    """The format filled with made-up goods, as a PDF — to see that the AI's reading puts each figure in the right place."""
    try:
        entry = stock_templates.get(ident)
        report = stock_report.render(entry.template, _PREVIEW_ORDER, f"preview-{entry.id}")
    except TemplateError as exc:
        raise _fail(exc, 404) from exc
    except StockReportError as exc:
        raise _fail(exc, 500) from exc
    return FileResponse(report.pdf_path, media_type=stock_report.PDF_MIME, content_disposition_type="inline")
