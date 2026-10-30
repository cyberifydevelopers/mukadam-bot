"""Answers a question typed on WhatsApp about the reports ("what is margin", "which factory is this from",
"what was the previous balance of cotton", "rate of putti?").

Only facts the bot really holds go to the model — the document, its stock report (as made, with the figures that were
computed), the company's previous report, the figures kept per factory and goods — and it is told to answer from those
alone and to say so when something isn't there, never to guess a figure. Nothing is written by a question.
"""

import json
import logging
import re
from pathlib import Path

import openpyxl
from sqlalchemy.orm import Session

from app.config import settings
from app.models import GoodsProfile, InvoiceRecord
from app.services import goods_profiles, stock_report, stock_update

logger = logging.getLogger(__name__)

_MAX_REPORT_CHARS = 5000
_RECENT = 15

_SYSTEM = """You are the assistant of Muccaddam Bot, which turns bank delivery orders / L-C letters into stock reports \
(PDF + Excel) for a muccadam company. You are chatting on WhatsApp with the person who approves them.

Answer the question ONLY from the DATA below. Rules:
- Never invent or estimate a figure. If the data does not hold the answer, say what is missing and how to give it: \
reply to the stock report with e.g. "margin wheat 5", "rate cotton 85000", "weight 50" or "drums 120"; or ask for \
"report 16" / "original pdf 16".
- A figure shown as empty / 0 / missing in the report was not given by the document: say so plainly.
- "Source" means where the document came from: the bank, its branch, the sender's email, the factory (customer) and the \
godown. Say which of these the data has.
- If a question is about "this report" it means FOCUS REPORT. Without one, use the most recent report or ask which \
report number (e.g. "report 16"), and say which one you used.
- ANSWER DIRECTLY. The first line is the answer itself: the figure(s) asked for, per goods, e.g. "Margin: Putti 20%, \
Cotton Bales 25%, Oil Cake 20%". "What is margin / rate / balance (in this)" asks for the VALUE in the report, not a \
definition. Do not explain what a term means, give examples, or add background unless the person clearly asks what \
it means ("margin kya hota hai", "what does DP mean", "explain"). Never add a definition line to a plain value question. \
After the answer add a line only about the DATA, and only if it matters (e.g. which goods have no figure, or which \
report it is from).
- When asked to explain a term, normally: Total Value = balance on hand x rate; Less Margin = the margin % of that \
value; Drawing Power (DP) = value minus the margin. Check the actual numbers in the report before saying it.
- Keep it short and clear for WhatsApp: no headings or tables, "•" for lists, *bold* only for key figures. Give amounts \
with thousand separators and the unit/currency. Reply in the language AND script the person wrote in: English gets English, \
Roman Urdu (Urdu in English letters, e.g. "rate kya hai") gets Roman Urdu, never Urdu script; Urdu script gets Urdu."""


def whatsapp_format(text: str) -> str:
    """WhatsApp bolds with *one* asterisk and has no headings: **bold** and '## Title' from the model are converted."""
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text, flags=re.DOTALL)
    return re.sub(r"(?m)^\s*#{1,6}\s*", "", text)


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 20] + "\n…(cut)"


def _sheet_text(xlsx_path: str) -> str:
    """The finished report's sheet as lines of 'cell | cell | …', with the figures as they were computed."""
    try:
        workbook = openpyxl.load_workbook(xlsx_path, data_only=True, read_only=True)
    except Exception:  # noqa: BLE001 — a missing / unreadable file just means no report text
        logger.exception("Couldn't read %s for a question", xlsx_path)
        return ""
    lines = []
    try:
        for row in workbook.worksheets[0].iter_rows(max_row=90, values_only=True):
            cells = [re.sub(r"\s+", " ", str(v)).strip()[:90] for v in row if v is not None and str(v).strip()]
            if cells:
                lines.append(" | ".join(cells))
    finally:
        workbook.close()  # a read-only workbook keeps the file open (locked on Windows) until closed
    return _cap("\n".join(lines), _MAX_REPORT_CHARS)


def _facts(record: InvoiceRecord) -> dict:
    raw = json.loads(record.raw_extracted_json or "{}")
    order = raw.get("delivery_order") or {}
    email = record.email
    facts = {
        "report_no": record.id,
        "status": record.status.value,
        "bank": record.bank_name or record.bank_code,
        "bank_branch": order.get("bank_branch"),
        "addressed_to": order.get("addressed_to"),
        "factory_customer": stock_report._customer(order) if order else None,
        "godown_pledge_location": order.get("pledge_location"),
        "document": raw.get("document_type"),
        "issuer": raw.get("issuer_name"),
        "reference": record.reference_number,
        "document_date": record.txn_date,
        "currency": order.get("currency"),
        "usd_to_pkr": raw.get("pkr"),
        "email_from": email.sender if email else None,
        "email_subject": email.subject if email else None,
        "email_received": email.received_at.isoformat(timespec="minutes") if email and email.received_at else None,
        "confirmed_at": record.confirmed_at.isoformat(timespec="minutes") if record.confirmed_at else None,
        "file": record.pdf_filename.split("_", 1)[-1],
        "stock_lines": [{k: v for k, v in row.items() if v not in (None, "")} for row in order.get("rows") or []],
        "missing_from_report": json.loads(record.stock_missing) if record.stock_missing else [],
    }
    return {k: v for k, v in facts.items() if v not in (None, "", [])}


def _previous(record: InvoiceRecord, order: dict) -> tuple[str, str] | None:
    """(date, sheet text) of the company's report that came just before this one."""
    try:
        template = stock_report.pick_template(record, order)
        own = stock_report._own_report(record.id)
        before = stock_report._order_key(own[0]) if own else ("9999", "")
        found = stock_report._previous_report(stock_report._customer(order), template, before, record.id)
    except Exception:  # noqa: BLE001 — no previous report to talk about
        return None
    if not found:
        return None
    state, xlsx = found
    return state.get("date", "?"), _sheet_text(xlsx)


def _profiles(db: Session, customer_key: str | None) -> list[dict]:
    query = db.query(GoodsProfile)
    if customer_key:
        query = query.filter(GoodsProfile.customer_key == customer_key)
    return [
        {"factory": p.customer_name or p.customer_key, "goods": p.goods_label or p.goods_key,
         **{label: getattr(p, field) for field, label in goods_profiles.FIELDS.items() if getattr(p, field)}}
        for p in query.order_by(GoodsProfile.customer_name).limit(60)
    ]


def _recent(db: Session) -> list[dict]:
    items = []
    for r in db.query(InvoiceRecord).order_by(InvoiceRecord.id.desc()).limit(80):
        order = stock_report.delivery_order(r)
        if not order:
            continue
        items.append({
            "report_no": r.id, "factory": stock_report._customer(order), "bank": r.bank_name or r.bank_code,
            "date": r.txn_date, "status": r.status.value, "reference": r.reference_number,
            "goods": [row.get("description") for row in order["rows"]],
        })
        if len(items) >= _RECENT:
            break
    return items


def build_context(db: Session, record: InvoiceRecord | None, text: str) -> str:
    """Everything the model may answer from, as plain text."""
    parts = []
    focus = record
    if focus is None:  # no report named: the latest one is the likeliest meaning of "the report"
        focus = next((r for r in db.query(InvoiceRecord).order_by(InvoiceRecord.id.desc()).limit(80) if stock_report.delivery_order(r)), None)
        if focus is not None:
            parts.append(f"(No report was named; the most recent one, #{focus.id}, is shown as FOCUS REPORT.)")

    factory = stock_update.factory_in_text(db, text)
    customer_key = None
    if focus is not None:
        order = stock_report.delivery_order(focus) or {}
        customer_key = goods_profiles.customer_key(order) if order else None
        parts.append("FOCUS REPORT (facts from the document):\n" + json.dumps(_facts(focus), ensure_ascii=False, indent=1))
        report = stock_report.latest(focus) if order else None
        if report and report.xlsx_path:
            parts.append("FOCUS REPORT as made (stock report sheet, computed figures):\n" + _sheet_text(report.xlsx_path))
        elif order:
            parts.append("No stock report file has been made for the focus report yet.")
        previous = _previous(focus, order) if order else None
        if previous:
            parts.append(f"PREVIOUS REPORT of the same company (dated {previous[0]}):\n{previous[1]}")
        else:
            parts.append("There is no earlier report of this company.")
    if factory:
        customer_key = factory[0]
        parts.append(f"The question names the factory: {factory[1]}")
    saved = _profiles(db, customer_key)
    parts.append("FIGURES KEPT PER FACTORY AND GOODS (margin / weight / rate used for new orders):\n"
                 + (json.dumps(saved, ensure_ascii=False) if saved else "none saved"))
    parts.append("RECENT REPORTS (newest first):\n" + json.dumps(_recent(db), ensure_ascii=False))
    return "\n\n".join(parts)


class AnswerError(Exception):
    pass


def answer(db: Session, text: str, record: InvoiceRecord | None) -> str:
    """The reply to send. Raises AnswerError (with words fit for the person) when the AI service can't be used."""
    from app.parsers import llm_parser  # late: the parser module is heavy and imports settings at load

    try:
        client = llm_parser.openrouter_client()
        response = llm_parser.chat_with_fallback(
            client,
            temperature=0,
            max_tokens=700,
            timeout=60,
            messages=[
                {"role": "system", "content": _SYSTEM + "\n\nDATA:\n" + build_context(db, record, text)},
                {"role": "user", "content": text},
            ],
        )
    except llm_parser.LlmParseError as exc:
        raise AnswerError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — any provider failure is told to the person, not raised
        logger.exception("Answering a WhatsApp question failed")
        raise AnswerError("I couldn't answer that just now (the AI service didn't respond). Please try again in a minute.") from exc
    reply = (response.choices[0].message.content or "").strip()
    if not reply:
        raise AnswerError("I couldn't find an answer to that in the reports.")
    return whatsapp_format(reply)
