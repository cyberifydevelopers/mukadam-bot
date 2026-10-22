import json
import logging
import mimetypes
import os
import re
import threading

from fastapi import APIRouter, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.config import settings
from app.confirmation import (
    Confirmable,
    apply_answer,
    find_by_label,
    find_by_message_id,
    label_of,
    pending_for_number,
)
from app.database import SessionLocal
from app.models import InvoiceRecord, InvoiceStatus, StatementTransaction, WhatsAppMessage
from app.services import openwa_service, stock_report
from app.services.message_log import apply_delivery_status, log_message

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook/openwa", tags=["openwa"])


def _digits(number: str | None) -> str:
    return re.sub(r"\D", "", number or "")


def _link_ids(target: Confirmable | None) -> dict:
    if isinstance(target, StatementTransaction):
        return {"transaction_id": target.id, "invoice_id": target.invoice_id}
    return {"invoice_id": target.id if target else None}


def _find_target(db: Session, reply: openwa_service.TextReply) -> tuple[Confirmable | None, str | None]:
    """Resolves which invoice/transaction a typed YES/NO answers. Returns
    (target, None), or (None, message-to-send-back) when the approver needs
    to be more specific.

    1. A quoted reply names the prompt's message id directly — only the chat
       the prompt was sent to can quote it, so this alone identifies it.
    2. Otherwise the sender must be the number the prompt went to, and either
       named it ("YES T12" / "YES 7") or has exactly one thing pending.
    """
    if reply.quoted_message_id:
        target = find_by_message_id(db, reply.quoted_message_id)
        if target is not None:
            return target, None

    if not reply.sender_phone:
        logger.warning("OpenWA reply %s has no resolvable sender phone; ignored", reply.message_id)
        return None, None

    if reply.target_label:
        target = find_by_label(db, reply.target_label)
        if target is not None and _digits(target.whatsapp_to_number) == reply.sender_phone:
            if target.status == InvoiceStatus.PENDING_CONFIRMATION:
                return target, None
            if target.status in (InvoiceStatus.CONFIRMED, InvoiceStatus.REJECTED):
                done = "saved" if target.status == InvoiceStatus.CONFIRMED else "skipped"
                return None, f"{label_of(target)} was already {done}."
        return None, f"Nothing pending as {reply.target_label} for you."

    mine = pending_for_number(db, reply.sender_phone)
    if len(mine) == 1:
        return mine[0], None
    if not mine:
        return None, None  # a stray "yes" from someone with nothing pending — stay silent
    return None, (
        f"You have {len(mine)} items waiting. Reply to the specific message with YES or NO, "
        f"or send e.g. YES {label_of(mine[0]).lstrip('#')}."
    )


def _send_logged(db: Session, to_number: str, text: str, target: Confirmable | None) -> None:
    message_id = error = None
    try:
        message_id = openwa_service.send_text(to_number, text)
    except openwa_service.OpenWaSendError as exc:
        error = str(exc)
        logger.error("OpenWA send to %s failed: %s", to_number, exc)
    log_message(
        db,
        direction="out",
        provider="openwa",
        peer_number=to_number,
        text=text,
        status="failed" if error else "sent",
        message_id=message_id,
        error=error,
        **_link_ids(target),
    )


def _acknowledge(db: Session, target: Confirmable, answer: str, ack: str) -> None:
    """Statement transactions come in dozens, so they're acknowledged with the
    bot's own ✅/❌ reaction on the prompt rather than a message each. A
    single invoice gets a text reply. Falls back to text if reacting fails."""
    if isinstance(target, StatementTransaction):
        try:
            openwa_service.react(target.whatsapp_to_number, target.whatsapp_message_id, "✅" if answer == "yes" else "❌")
            return
        except openwa_service.OpenWaSendError as exc:
            logger.warning("Couldn't react to %s (%s); sending text instead", target.label, exc)
    _send_logged(db, target.whatsapp_to_number, ack, target)


def _send_stock_report(db: Session, record: InvoiceRecord) -> None:
    """After a delivery order is confirmed: builds its stock report and sends
    it back to the approver as PDF + Excel. A failure here is reported in the
    chat but never undoes the save — the YES already went to the Sheet."""
    to_number = record.whatsapp_to_number
    try:
        report = stock_report.build(record)
    except Exception as exc:  # noqa: BLE001 — any failure is reported to the approver, not raised
        logger.exception("Stock report for #%s failed", record.id)
        _send_logged(db, to_number, f"Saved, but the stock report for #{record.id} couldn't be made: {exc}", record)
        return

    files = [(report.pdf_path, stock_report.PDF_MIME), (report.xlsx_path, stock_report.XLSX_MIME)]
    files = [(path, mime) for path, mime in files if path]
    for n, (path, mimetype) in enumerate(files):
        caption = f"Stock report for #{record.id} — {report.customer}" if n == 0 else None
        message_id = error = None
        try:
            message_id = openwa_service.send_document(to_number, path, mimetype, caption)
        except openwa_service.OpenWaSendError as exc:
            error = str(exc)
            logger.error("OpenWA document send to %s failed: %s", to_number, exc)
        log_message(
            db,
            direction="out",
            provider="openwa",
            peer_number=to_number,
            text=f"📎 {os.path.basename(path)}" + (f"\n{caption}" if caption else ""),
            status="failed" if error else "sent",
            message_id=message_id,
            error=error,
            invoice_id=record.id,
        )


def _handle_pdf_request(db: Session, request: openwa_service.PdfRequest) -> dict:
    """"Share pdf for 496": sends the stock report (PDF) made for that item
    back to the person asking — "original pdf 496" sends the document as it
    arrived instead. Only the approver numbers (NOTIFY_WHATSAPP_NUMBERS) can
    ask — these are bank documents — and anyone else is ignored silently."""
    if not request.sender_phone or request.sender_phone not in {_digits(n) for n in settings.notify_numbers_list}:
        return {"status": "ignored"}
    if request.message_id and (
        db.query(WhatsAppMessage.id).filter_by(direction="in", message_id=request.message_id).first()
    ):
        logger.info("OpenWA re-delivered message %s — already handled, ignored", request.message_id)
        return {"status": "duplicate"}

    target = None
    if request.target_label:
        target = find_by_label(db, request.target_label)
    elif request.quoted_message_id:
        target = find_by_message_id(db, request.quoted_message_id)
    record = db.get(InvoiceRecord, target.invoice_id) if isinstance(target, StatementTransaction) else target
    log_message(
        db,
        direction="in",
        provider="openwa",
        peer_number=request.sender_phone,
        text=request.text,
        status="received",
        message_id=request.message_id,
        **_link_ids(record),
    )

    asked = f"#{request.target_label}" if request.target_label else "that message"
    if record is None:
        _send_logged(db, request.sender_phone, f"I couldn't find {asked}.", None)
        db.commit()
        return {"status": "ok"}

    if request.original:
        path = os.path.join(settings.pdf_storage_dir, record.pdf_filename)
        if not os.path.exists(path):
            _send_logged(db, request.sender_phone, f"The file for #{record.id} is no longer on the server.", record)
            db.commit()
            return {"status": "ok"}
        # Stored as "<hash>_<original name>"; the person gets the original name.
        name = record.pdf_filename.split("_", 1)[-1]
        caption = f"Original document of #{record.id}"
    else:
        if not stock_report.delivery_order(record):
            _send_logged(
                db,
                request.sender_phone,
                f"#{record.id} has no stock report — only delivery orders and L/C letters get one. "
                f"Send \"original pdf {record.id}\" for the document itself.",
                record,
            )
            db.commit()
            return {"status": "ok"}
        try:
            report = stock_report.latest(record) or stock_report.build(record)
        except Exception as exc:  # noqa: BLE001 — reported to the person asking, not raised
            logger.exception("Stock report for #%s failed", record.id)
            _send_logged(db, request.sender_phone, f"The stock report for #{record.id} couldn't be made: {exc}", record)
            db.commit()
            return {"status": "ok"}
        path = report.pdf_path
        name = os.path.basename(path)
        caption = f"Stock report for #{record.id} — {report.customer}"
    message_id = error = None
    try:
        message_id = openwa_service.send_document(
            request.sender_phone, path, mimetypes.guess_type(name)[0] or "application/pdf", caption, filename=name
        )
    except openwa_service.OpenWaSendError as exc:
        error = str(exc)
        logger.error("OpenWA document send to %s failed: %s", request.sender_phone, exc)
    log_message(
        db,
        direction="out",
        provider="openwa",
        peer_number=request.sender_phone,
        text=f"📎 {name}\n{caption}",
        status="failed" if error else "sent",
        message_id=message_id,
        error=error,
        invoice_id=record.id,
    )
    if error:
        _send_logged(db, request.sender_phone, f"I couldn't send the file for #{record.id}: {error}", record)
    db.commit()
    return {"status": "ok"}


def _answer(db: Session, target: Confirmable, answer: str) -> None:
    # None = already handled (OpenWA retried the delivery, or a second
    # reaction on the same prompt) — idempotent no-op.
    ack = apply_answer(db, target, answer)
    db.commit()
    if ack is not None:
        _acknowledge(db, target, answer, ack)
        db.commit()
        if answer == "yes" and isinstance(target, InvoiceRecord) and stock_report.delivery_order(target):
            _send_stock_report(db, target)
            db.commit()


# Deliveries are handled one at a time: OpenWA re-sends a webhook it
# didn't get a quick answer to, and a re-sent "YES 7" must see the first
# one's result (and be dropped as a duplicate) rather than race it.
_HANDLE_LOCK = threading.Lock()


@router.post("")
async def receive_webhook(request: Request, token: str = Query(default="")):
    """OpenWA webhook (registered for `message.received`, `message.reaction`
    and `message.ack`). Authenticated by OpenWA's signature header, or by a
    `?token=` in the URL for a webhook created without a secret."""
    raw_body = await request.body()
    if not openwa_service.verify_webhook_signature(raw_body, request.headers.get("X-OpenWA-Signature"), token):
        logger.warning("Rejected OpenWA webhook call with invalid/missing signature")
        return Response(status_code=403)

    # Handling blocks for seconds (Sheets write, sending the ack). Run on the
    # event loop, it froze the whole server, so OpenWA's deliveries timed out
    # and were re-sent — each re-sent YES then got a "Nothing pending" reply.
    return await run_in_threadpool(_handle, json.loads(raw_body))


def _handle(payload: dict) -> dict:
    with _HANDLE_LOCK, SessionLocal() as db:
        return _handle_payload(db, payload)


def _handle_payload(db: Session, payload: dict) -> dict:
    if payload.get("event") == "message.ack":
        data = payload.get("data") or {}
        if data.get("messageId") and apply_delivery_status(db, data["messageId"], data.get("status", "")):
            db.commit()
        return {"status": "ok"}

    if reaction := openwa_service.parse_reaction(payload):
        # Only the chat a prompt was sent to can react to it, so the reacted-to
        # message id alone identifies what's being answered.
        target = find_by_message_id(db, reaction.message_id)
        if target is None:
            return {"status": "ignored"}
        if target.status == InvoiceStatus.PENDING_CONFIRMATION:
            log_message(
                db,
                direction="in",
                provider="openwa",
                peer_number=target.whatsapp_to_number,
                text=f"Reacted {reaction.emoji} to {label_of(target)}",
                status="received",
                **_link_ids(target),
            )
        _answer(db, target, reaction.answer)
        return {"status": "ok"}

    if pdf_request := openwa_service.parse_pdf_request(payload):
        return _handle_pdf_request(db, pdf_request)

    reply = openwa_service.parse_reply(payload)
    if reply is None:
        return {"status": "ignored"}
    if reply.message_id and (
        db.query(WhatsAppMessage.id).filter_by(direction="in", message_id=reply.message_id).first()
    ):
        logger.info("OpenWA re-delivered message %s — already handled, ignored", reply.message_id)
        return {"status": "duplicate"}

    target, message = _find_target(db, reply)
    log_message(
        db,
        direction="in",
        provider="openwa",
        peer_number=reply.sender_phone or None,
        text=reply.text,
        status="received",
        message_id=reply.message_id,
        **_link_ids(target),
    )

    if target is None:
        if message and reply.sender_phone in {_digits(n) for n in settings.notify_numbers_list}:
            _send_logged(db, reply.sender_phone, message, None)
        db.commit()
        return {"status": "ok"}

    _answer(db, target, reply.answer)
    return {"status": "ok"}
