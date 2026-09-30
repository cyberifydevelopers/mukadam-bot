"""JSON API behind the dashboard (app/static/index.html).

The app has no login: it is meant to run on 127.0.0.1 only. These routes
expose email subjects, extracted financial data and decrypted PDFs, so do
not bind it to a public interface without putting auth in front.
"""

import json
import logging
import os
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import runtime_settings
from app.bank_registry import BANK_REGISTRY, profile_by_code
from app.config import settings
from app.database import get_db
from app.email_view import render_email_html
from app.models import (
    GmailSyncState,
    InvoiceRecord,
    InvoiceStatus,
    ProcessedEmail,
    StatementTransaction,
    WhatsAppMessage,
)
from app.parsers.statement_rows import parse_statement
from app.pipeline import reparse_invoice, resend_confirmation, retry_email, sync_gmail
from app.services import openwa_service
from app.services.document_reader import read_document
from app.services.pdf_extractor import PdfExtractionError
from app.worker import pubsub_listener

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["dashboard"])


def _iso(dt: datetime | None) -> str | None:
    # Stored as naive UTC; the "Z" lets the browser show local time.
    return dt.isoformat() + "Z" if dt else None


def _invoice_json(r: InvoiceRecord) -> dict:
    return {
        "id": r.id,
        "email_id": r.email_id,
        "email_subject": r.email.subject if r.email else None,
        "email_sender": r.email.sender if r.email else None,
        "pdf_filename": r.pdf_filename,
        "bank_code": r.bank_code,
        "bank_name": r.bank_name or profile_by_code(r.bank_code).display_name,
        "txn_date": r.txn_date,
        "amount": r.amount,
        "sender_name": r.sender_name,
        "receiver_name": r.receiver_name,
        "reference_number": r.reference_number,
        "extra": json.loads(r.raw_extracted_json) if r.raw_extracted_json else None,
        "parse_error": r.parse_error,
        "status": r.status.value,
        "whatsapp_to_number": r.whatsapp_to_number,
        "created_at": _iso(r.created_at),
        "confirmed_at": _iso(r.confirmed_at),
        "transaction_counts": _txn_counts(r.transactions) if r.transactions else None,
    }


def _txn_counts(txns: list[StatementTransaction]) -> dict:
    counts: dict[str, int] = {"total": len(txns)}
    for t in txns:
        counts[t.status.value] = counts.get(t.status.value, 0) + 1
    return counts


def _txn_json(t: StatementTransaction) -> dict:
    return {
        "id": t.id,
        "label": t.label,
        "invoice_id": t.invoice_id,
        "bank_code": t.bank_code,
        "row_no": t.row_no,
        "txn_date": t.txn_date,
        "details": t.details,
        "party": t.party,
        "reference": t.reference,
        "debit": t.debit,
        "credit": t.credit,
        "balance": t.balance,
        "reconciled": t.reconciled,
        "status": t.status.value,
        "whatsapp_to_number": t.whatsapp_to_number,
        "created_at": _iso(t.created_at),
        "confirmed_at": _iso(t.confirmed_at),
    }


def _message_json(m: WhatsAppMessage) -> dict:
    return {
        "id": m.id,
        "invoice_id": m.invoice_id,
        "transaction_id": m.transaction_id,
        "direction": m.direction,
        "provider": m.provider,
        "peer_number": m.peer_number,
        "text": m.text,
        "status": m.status,
        "error": m.error,
        "created_at": _iso(m.created_at),
        "updated_at": _iso(m.updated_at),
    }


def _get_invoice(db: Session, invoice_id: int) -> InvoiceRecord:
    record = db.get(InvoiceRecord, invoice_id)
    if record is None:
        raise HTTPException(404, "Invoice not found")
    return record


def _pdf_path(record: InvoiceRecord) -> str:
    return os.path.join(settings.pdf_storage_dir, record.pdf_filename)


@router.get("/overview")
def overview(db: Session = Depends(get_db)):
    status_counts = dict(
        db.query(InvoiceRecord.status, func.count()).group_by(InvoiceRecord.status).all()
    )
    msg_counts = dict(
        db.query(WhatsAppMessage.status, func.count())
        .filter(WhatsAppMessage.direction == "out")
        .group_by(WhatsAppMessage.status)
        .all()
    )
    txn_counts = dict(
        db.query(StatementTransaction.status, func.count()).group_by(StatementTransaction.status).all()
    )
    return {
        "emails": db.query(func.count(ProcessedEmail.id)).scalar(),
        "transactions": {s.value: txn_counts.get(s, 0) for s in InvoiceStatus},
        "invoices": {s.value: status_counts.get(s, 0) for s in InvoiceStatus},
        "whatsapp_out": msg_counts,
        "whatsapp_in": db.query(func.count(WhatsAppMessage.id)).filter(WhatsAppMessage.direction == "in").scalar(),
        "recent_invoices": [
            _invoice_json(r)
            for r in db.query(InvoiceRecord).order_by(InvoiceRecord.created_at.desc()).limit(5)
        ],
    }


@router.get("/status")
def status(db: Session = Depends(get_db)):
    state = db.get(GmailSyncState, 1)
    return {
        "gmail": {
            "initialized": state is not None,
            "last_history_id": state.last_history_id if state else None,
            "last_sync_at": _iso(state.updated_at) if state else None,
            "watch_expires_at": (
                _iso(datetime.utcfromtimestamp(state.watch_expiration_ms / 1000))
                if state and state.watch_expiration_ms
                else None
            ),
            "reconcile_interval_seconds": settings.gmail_reconcile_interval_seconds,
            "pubsub": {**pubsub_listener.status, "subscription": pubsub_listener.subscription_path() or None},
            "labels": settings.gmail_watch_label_ids,
        },
        "whatsapp": {
            "provider": settings.whatsapp_provider,
            "notify_numbers": settings.notify_numbers_list,
            "notify_numbers_source": runtime_settings.notify_numbers_source(db),
            "openwa": openwa_service.session_status() if settings.whatsapp_provider == "openwa" else None,
        },
        "sheets": {
            "configured": bool(settings.google_sheets_spreadsheet_id),
            "url": (
                f"https://docs.google.com/spreadsheets/d/{settings.google_sheets_spreadsheet_id}/edit"
                if settings.google_sheets_spreadsheet_id
                else None
            ),
        },
        "llm": {"model": settings.llm_extraction_model, "configured": bool(settings.openrouter_api_key)},
        "banks": [
            {
                "code": b.bank_code,
                "name": b.display_name,
                "sender_match": list(b.sender_match),
                "parser": type(b.parser).__name__ if b.parser else "LLM",
                "password_env": b.pdf_password_env,
                "password_set": bool(b.pdf_password_env and os.environ.get(b.pdf_password_env)),
            }
            for b in BANK_REGISTRY
        ],
    }


@router.post("/sync")
def sync_now(db: Session = Depends(get_db)):
    try:
        return {"new_invoices_queued": sync_gmail(db)}
    except Exception as exc:
        # Usually a network failure talking to Gmail. The sync cursor is only
        # advanced on success, so nothing is skipped — just try again.
        logger.exception("Manual Gmail sync failed")
        db.rollback()
        raise HTTPException(502, f"Gmail sync failed: {type(exc).__name__}: {exc}") from exc


@router.post("/invoices/{invoice_id}/reparse")
def reparse(invoice_id: int, db: Session = Depends(get_db)):
    record = _get_invoice(db, invoice_id)
    retryable = (InvoiceStatus.FAILED_PARSE, InvoiceStatus.SEND_FAILED, InvoiceStatus.PENDING_CONFIRMATION)
    if record.status not in retryable:
        raise HTTPException(409, f"Invoice is {record.status.value}; it can't be re-parsed")
    reparse_invoice(db, record)
    return _invoice_json(record)


@router.get("/emails")
def list_emails(db: Session = Depends(get_db)):
    emails = db.query(ProcessedEmail).order_by(ProcessedEmail.received_at.desc()).all()
    return [
        {
            "id": e.id,
            "message_id": e.message_id,
            "sender": e.sender,
            "subject": e.subject,
            "received_at": _iso(e.received_at),
            "processed_at": _iso(e.processed_at),
            "classification": e.classification,
            "bank_name": e.bank_name,
            "classification_reason": e.classification_reason,
            "invoices": [
                {"id": r.id, "pdf_filename": r.pdf_filename, "bank_code": r.bank_code, "status": r.status.value}
                for r in e.invoices
            ],
        }
        for e in emails
    ]


@router.get("/invoices")
def list_invoices(db: Session = Depends(get_db)):
    return [_invoice_json(r) for r in db.query(InvoiceRecord).order_by(InvoiceRecord.created_at.desc())]


@router.get("/invoices/{invoice_id}")
def get_invoice(invoice_id: int, db: Session = Depends(get_db)):
    record = _get_invoice(db, invoice_id)
    messages = (
        db.query(WhatsAppMessage)
        .filter(WhatsAppMessage.invoice_id == invoice_id)
        .order_by(WhatsAppMessage.created_at)
        .all()
    )
    return {
        **_invoice_json(record),
        "messages": [_message_json(m) for m in messages],
        "transactions": [_txn_json(t) for t in record.transactions],
    }


@router.get("/invoices/{invoice_id}/content")
def invoice_content(invoice_id: int, db: Session = Depends(get_db)):
    """What was read from the attachment: its text (PDF text layer, or the
    spreadsheet's cells), plus its transaction rows when it's an account
    statement. A scanned PDF has no text — the LLM read its pages."""
    record = _get_invoice(db, invoice_id)
    doc = read_document(_pdf_path(record))
    return {
        "kind": doc.kind,
        "text": doc.text,
        "error": doc.error,
        "statement": parse_statement(doc.text) if doc.kind == "pdf_text" else None,
    }


@router.get("/invoices/{invoice_id}/pdf")
def invoice_pdf(invoice_id: int, db: Session = Depends(get_db)):
    """The attachment as received — a PDF with its password removed so the
    browser can display it; an Excel/CSV file as a download."""
    record = _get_invoice(db, invoice_id)
    doc = read_document(_pdf_path(record))
    if doc.filename.lower().endswith(".pdf"):
        try:
            data = doc.pdf_bytes
        except PdfExtractionError as exc:
            raise HTTPException(422, str(exc)) from exc
        return Response(
            data,
            media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="invoice-{record.id}.pdf"'},
        )
    if doc.kind == "email":
        # The body of an email that had no attachment — show it as a readable page.
        images = [f"/api/invoices/{record.id}/images/{i}" for i in range(len(doc.image_paths))]
        title = record.email.subject if record.email else f"Email #{record.id}"
        return HTMLResponse(render_email_html(doc.text or "", title, images))
    return FileResponse(doc.path, filename=doc.filename)


@router.get("/invoices/{invoice_id}/images/{index}")
def invoice_image(invoice_id: int, index: int, db: Session = Depends(get_db)):
    """A picture (logo) from an email body document."""
    record = _get_invoice(db, invoice_id)
    doc = read_document(_pdf_path(record))
    if not 0 <= index < len(doc.image_paths):
        raise HTTPException(404, "No such image")
    return FileResponse(doc.image_paths[index])


@router.post("/invoices/{invoice_id}/resend")
def resend(invoice_id: int, db: Session = Depends(get_db)):
    record = _get_invoice(db, invoice_id)
    if record.status not in (InvoiceStatus.SEND_FAILED, InvoiceStatus.PENDING_CONFIRMATION):
        raise HTTPException(409, f"Invoice is {record.status.value}; only failed or pending prompts can be resent")
    resend_confirmation(db, record)
    return _invoice_json(record)


@router.get("/whatsapp/messages")
def list_messages(db: Session = Depends(get_db)):
    return [
        _message_json(m)
        for m in db.query(WhatsAppMessage).order_by(WhatsAppMessage.created_at.desc()).limit(500)
    ]


class NotifyNumbersIn(BaseModel):
    numbers: str  # one or more, comma-separated


@router.put("/whatsapp/notify-numbers")
def update_notify_numbers(body: NotifyNumbersIn, db: Session = Depends(get_db)):
    """Changes who receives the confirmation prompts, from the next prompt
    on. Prompts already sent stay tied to the number they went to."""
    try:
        numbers = runtime_settings.set_notify_numbers(db, body.numbers)
    except runtime_settings.InvalidSetting as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"notify_numbers": numbers}


@router.get("/transactions")
def list_transactions(status: str | None = None, db: Session = Depends(get_db)):
    q = db.query(StatementTransaction)
    if status:
        q = q.filter(StatementTransaction.status == InvoiceStatus(status))
    return [_txn_json(t) for t in q.order_by(StatementTransaction.id.desc()).limit(1000)]


@router.delete("/invoices/{invoice_id}")
def delete_invoice(invoice_id: int, db: Session = Depends(get_db)):
    """Deletes an extracted file: its record, its statement transactions and
    the file on disk. The WhatsApp message log is kept (unlinked), and rows
    already saved to the Google Sheet are not touched. The email itself stays
    marked as processed, so it isn't fetched and extracted again."""
    record = _get_invoice(db, invoice_id)
    txn_ids = [t.id for t in record.transactions]
    q = db.query(WhatsAppMessage).filter(WhatsAppMessage.invoice_id == record.id)
    if txn_ids:
        q = db.query(WhatsAppMessage).filter(
            (WhatsAppMessage.invoice_id == record.id) | (WhatsAppMessage.transaction_id.in_(txn_ids))
        )
    q.update({WhatsAppMessage.invoice_id: None, WhatsAppMessage.transaction_id: None}, synchronize_session=False)
    for txn in record.transactions:
        db.delete(txn)
    path = _pdf_path(record)
    db.delete(record)
    db.commit()
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("Deleted invoice %s but couldn't remove %s: %s", invoice_id, path, exc)
    return {"deleted": invoice_id}


@router.get("/activity")
def activity(db: Session = Depends(get_db)):
    """The latest emails with where each is in processing — for the live
    table on the Overview page. Per document: its status and the delivery
    state (sent / delivered / read / failed) of its WhatsApp prompt."""
    emails = db.query(ProcessedEmail).order_by(ProcessedEmail.id.desc()).limit(25).all()
    prompt_ids = [r.whatsapp_message_id for e in emails for r in e.invoices if r.whatsapp_message_id]
    delivery = dict(
        db.query(WhatsAppMessage.message_id, WhatsAppMessage.status)
        .filter(WhatsAppMessage.message_id.in_(prompt_ids))
        .all()
    ) if prompt_ids else {}

    def doc(r: InvoiceRecord) -> dict:
        extra = json.loads(r.raw_extracted_json) if r.raw_extracted_json else {}
        return {
            "id": r.id,
            "filename": r.pdf_filename.split("_", 1)[-1],
            "status": r.status.value,
            "kind": "statement" if r.transactions else extra.get("source_kind"),
            "document_type": extra.get("document_type"),
            "reference": r.reference_number,
            "amount": r.amount,
            "currency": extra.get("currency"),
            "transactions": len(r.transactions),
            "whatsapp": delivery.get(r.whatsapp_message_id) if r.whatsapp_message_id else None,
            "parse_error": r.parse_error,
        }

    def selection(e: ProcessedEmail) -> str:
        if e.stage in ("reading", "analyzing", "sending"):
            return "processing"
        if e.stage == "failed":
            return "failed"
        if e.stage in (None, "done") and not e.invoices:
            return "deleted"  # processed, then its files were deleted from the dashboard
        if e.stage in ("not_bank", "no_attachment") or e.classification == "not_bank":
            return "rejected"
        if e.classification == "bank" or any(r.status != InvoiceStatus.NOT_BANK for r in e.invoices):
            return "selected"
        return "rejected"

    return [
        {
            "id": e.id,
            "sender": e.sender,
            "subject": e.subject,
            "received_at": _iso(e.received_at),
            "selection": selection(e),
            "stage": e.stage or "legacy",  # processed before live stages existed
            "stage_detail": e.stage_detail,
            "stage_updated_at": _iso(e.stage_updated_at),
            "processed_at": _iso(e.processed_at),
            "notification_published_at": _iso(e.notification_published_at),
            "notification_received_at": _iso(e.notification_received_at),
            "classification": e.classification,
            "bank_name": e.bank_name,
            "classification_reason": e.classification_reason,
            "attachments": json.loads(e.attachment_names) if e.attachment_names else [],
            "documents": [doc(r) for r in e.invoices],
        }
        for e in emails
    ]


@router.post("/emails/{email_id}/retry")
def retry(email_id: int, db: Session = Depends(get_db)):
    email = db.get(ProcessedEmail, email_id)
    if email is None:
        raise HTTPException(404, "Email not found")
    if email.stage not in ("failed", "no_attachment", "not_bank"):
        raise HTTPException(409, f"Email is '{email.stage}' — only failed / skipped emails can be retried")
    try:
        created = retry_email(db, email)
    except Exception as exc:
        db.rollback()
        raise HTTPException(502, f"Retry failed: {type(exc).__name__}: {exc}") from exc
    return {"prompts_sent": created}


@router.get("/activity/version")
def activity_version(db: Session = Depends(get_db)):
    """A cheap fingerprint of the incoming-emails table — the Overview polls
    this every 2 s and re-renders only when it changes, so a new email (or a
    new processing step) shows up right away."""
    latest = db.query(func.max(ProcessedEmail.id), func.max(ProcessedEmail.stage_updated_at)).one()
    msgs = db.query(func.max(WhatsAppMessage.updated_at), func.count(WhatsAppMessage.id)).one()
    return {"v": f"{latest[0]}|{latest[1]}|{msgs[0]}|{msgs[1]}"}
