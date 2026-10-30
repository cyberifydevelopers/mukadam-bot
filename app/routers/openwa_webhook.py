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
from app.services import stock_update, openwa_service, pkr, stock_report
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


def _deliver_report(db: Session, to_number: str, record: InvoiceRecord, report: stock_report.StockReport) -> None:
    """Sends a stock report as its PDF and its Excel — always both — the caption (what was filled in / what is
    missing) going with the first."""
    files = [(report.pdf_path, stock_report.PDF_MIME), (report.xlsx_path, stock_report.XLSX_MIME)]
    for n, (path, mimetype) in enumerate(f for f in files if f[0]):
        caption = stock_report.caption(record, report) if n == 0 else None
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
        if error:
            _send_logged(db, to_number, f"I couldn't send {os.path.basename(path)} for #{record.id}: {error}", record)


def _send_stock_report(db: Session, record: InvoiceRecord) -> None:
    """After a delivery order is confirmed: builds its stock report and sends
    it back to the approver as PDF + Excel. A failure here is reported in the
    chat but never undoes the save — the YES already went to the Sheet."""
    to_number = record.whatsapp_to_number
    try:
        report = stock_report.build(record)
    except stock_report.NoFormatError as exc:
        logger.warning("Stock report for #%s not made: %s", record.id, exc)
        _send_logged(db, to_number, f"Saved. No stock report was made for #{record.id}. {exc}", record)
        return
    except Exception as exc:  # noqa: BLE001 — any failure is reported to the approver, not raised
        logger.exception("Stock report for #%s failed", record.id)
        _send_logged(db, to_number, f"Saved, but the stock report for #{record.id} couldn't be made: {exc}", record)
        return

    _deliver_report(db, to_number, record, report)


def _record_for_quote(db: Session, quoted_id: str | None) -> InvoiceRecord | None:
    """The document a quoted message belongs to: the prompt itself, or a stock report sent for it."""
    if not quoted_id:
        return None
    target = find_by_message_id(db, quoted_id)
    if isinstance(target, StatementTransaction):
        target = db.get(InvoiceRecord, target.invoice_id)
    if target is not None:
        return target
    row = db.query(WhatsAppMessage).filter(WhatsAppMessage.message_id == quoted_id, WhatsAppMessage.invoice_id.isnot(None)).first()
    return db.get(InvoiceRecord, row.invoice_id) if row else None


def _handle_figures_request(db: Session, request: openwa_service.FiguresRequest) -> dict:
    """"rate wheat 3800 / drums 120" sent as a reply to a stock report: the figures are stored on that
    document's stock lines and the report is made again with them. Only the approver numbers can send them."""
    if not request.sender_phone or request.sender_phone not in {_digits(n) for n in settings.notify_numbers_list}:
        return {"status": "ignored"}
    if request.message_id and (
        db.query(WhatsAppMessage.id).filter_by(direction="in", message_id=request.message_id).first()
    ):
        return {"status": "duplicate"}

    record = _record_for_quote(db, request.quoted_message_id)
    if record is None and request.target_label:
        record = db.get(InvoiceRecord, int(request.target_label))
    log_message(
        db, direction="in", provider="openwa", peer_number=request.sender_phone, text=request.text,
        status="received", message_id=request.message_id, **_link_ids(record),
    )
    if record is None:
        factory = stock_update.factory_in_text(db, request.text)
        if factory:
            notes = stock_update.save_profile_figures(db, factory[0], factory[1], request.figures)
            db.commit()
            if notes:
                reply = [f"Saved for {factory[1]}:", *(f"• {n}" for n in notes), "", "Used from the next invoice of these goods."]
            else:
                reply = [f"I couldn't tell which goods these are for {factory[1]}. Write e.g. \"{factory[1]} wheat margin 5\"."]
            _send_logged(db, request.sender_phone, "\n".join(reply), None)
            db.commit()
            return {"status": "ok"}
        _send_logged(db, request.sender_phone, "Which report or factory is this for? Reply to the stock report, or write e.g. \"report 16\" or \"ABC Factory wheat margin 5\" with the figures.", None)
        db.commit()
        return {"status": "ok"}
    if not stock_report.delivery_order(record):
        _send_logged(db, request.sender_phone, f"#{record.id} has no stock report, so there is nothing to add these to.", record)
        db.commit()
        return {"status": "ok"}

    applied, unmatched = stock_report.apply_figures(record, request.figures)
    db.commit()
    lines = [f"Got it for #{record.id}:", *(f"• {a}" for a in applied)]
    if unmatched:
        lines += ["", "Couldn't find these goods on the report: " + "; ".join(unmatched)]
    if applied:
        lines += ["", "Making the updated report…"]
    _send_logged(db, request.sender_phone, "\n".join(lines), record)
    db.commit()
    if applied:
        _send_stock_report(db, record)
        db.commit()
    return {"status": "ok"}


def _handle_pkr_request(db: Session, request: openwa_service.PkrRequest) -> dict:
    """"usd rate 280" / "pkr amount 1500000", the answer to a USD document's request for its rupee amount: kept on the
    document (as a reply to its message, "#12", or else the newest USD document still without one), and — for a
    delivery order whose stock report exists — the report is made again in rupees. Only the approver numbers."""
    if not request.sender_phone or request.sender_phone not in {_digits(n) for n in settings.notify_numbers_list}:
        return {"status": "ignored"}
    if request.message_id and (
        db.query(WhatsAppMessage.id).filter_by(direction="in", message_id=request.message_id).first()
    ):
        return {"status": "duplicate"}
    record = _record_for_quote(db, request.quoted_message_id)
    if record is None and request.target_label:
        record = db.get(InvoiceRecord, int(request.target_label))
    if record is None:
        record = pkr.latest_usd_record(db)
    log_message(
        db, direction="in", provider="openwa", peer_number=request.sender_phone, text=request.text,
        status="received", message_id=request.message_id, **_link_ids(record),
    )
    to = request.sender_phone
    if record is None:
        _send_logged(db, to, "Which document is this for? Reply to its message, or write e.g. \"usd rate 280 for #12\".", None)
        db.commit()
        return {"status": "ok"}
    if not pkr.is_usd(json.loads(record.raw_extracted_json or "{}")):
        _send_logged(db, to, f"#{record.id} is not in USD, so there is nothing to convert.", record)
        db.commit()
        return {"status": "ok"}
    given = pkr.apply(record, rate=request.rate, amount=request.amount)
    if given is None:
        _send_logged(db, to, f"I couldn't read the dollar amount of #{record.id}, so send the dollar rate instead, e.g. \"usd rate 280\".", record)
        db.commit()
        return {"status": "ok"}
    db.commit()
    lines = [f"Got it for #{record.id}: Rs {given['rate']:g} per dollar" + (f" → *PKR {given['amount']:,.0f}*." if given["amount"] else ".")]
    remake = record.status == InvoiceStatus.CONFIRMED and stock_report.delivery_order(record) is not None
    lines.append("Making the stock report again in rupees…" if remake else "It goes into the sheet in rupees too once you reply YES." if record.status == InvoiceStatus.PENDING_CONFIRMATION else "")
    _send_logged(db, to, "\n".join(l for l in lines if l), record)
    db.commit()
    if remake:
        _send_stock_report(db, record)
        db.commit()
    return {"status": "ok"}


def _handle_pdf_request(db: Session, request: openwa_service.PdfRequest) -> dict:
    """"Share pdf for 496" / "send report 496": sends the stock report made for that item back to the person
    asking, as its PDF and its Excel — "original pdf 496" sends the document as it arrived instead. Only the approver numbers (NOTIFY_WHATSAPP_NUMBERS) can
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
            report = stock_report.build(record, regenerate=True)  # made again, so a corrected format shows
        except stock_report.NoFormatError as exc:
            _send_logged(db, request.sender_phone, f"No stock report was made for #{record.id}. {exc}", record)
            db.commit()
            return {"status": "ok"}
        except Exception as exc:  # noqa: BLE001 — reported to the person asking, not raised
            logger.exception("Stock report for #%s failed", record.id)
            _send_logged(db, request.sender_phone, f"The stock report for #{record.id} couldn't be made: {exc}", record)
            db.commit()
            return {"status": "ok"}
        _deliver_report(db, request.sender_phone, record, report)
        db.commit()
        return {"status": "ok"}
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


def _spawn(fn, *args) -> None:
    threading.Thread(target=fn, args=args, daemon=True).start()


def _reply_to_question(to_number: str, text: str, record_id: int | None) -> None:
    """Works out and sends the answer. Runs on its own thread: the AI takes seconds, and a YES from the approver
    must not wait behind it (webhook deliveries are handled one at a time)."""
    from app.services import report_qa

    with SessionLocal() as db:
        record = db.get(InvoiceRecord, record_id) if record_id else None
        try:
            reply = report_qa.answer(db, text, record)
        except report_qa.AnswerError as exc:
            reply = str(exc)
        except Exception:  # noqa: BLE001 — the person always gets a reply, never silence
            logger.exception("Question %r failed", text)
            reply = "Sorry, something went wrong while looking that up."
        _send_logged(db, to_number, reply, record)
        db.commit()


def _handle_question(db: Session, question: openwa_service.Question) -> dict:
    """A question about the reports ("what is margin", "which factory is this from"), as a reply to a stock report
    or on its own. Only the approver numbers can ask — these are bank documents."""
    if not question.sender_phone or question.sender_phone not in {_digits(n) for n in settings.notify_numbers_list}:
        return {"status": "ignored"}
    if question.message_id and (
        db.query(WhatsAppMessage.id).filter_by(direction="in", message_id=question.message_id).first()
    ):
        return {"status": "duplicate"}
    record = _record_for_quote(db, question.quoted_message_id)
    if record is None and question.target_label:
        record = db.get(InvoiceRecord, int(question.target_label))
    log_message(
        db, direction="in", provider="openwa", peer_number=question.sender_phone, text=question.text,
        status="received", message_id=question.message_id, **_link_ids(record),
    )
    db.commit()
    _spawn(_reply_to_question, question.sender_phone, question.text, record.id if record else None)
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

    if pkr_request := openwa_service.parse_pkr_request(payload):  # before figures: "usd rate 280" is not a goods rate
        return _handle_pkr_request(db, pkr_request)

    if figures_request := openwa_service.parse_figures_request(payload):
        return _handle_figures_request(db, figures_request)

    reply = openwa_service.parse_reply(payload)
    if reply is None:
        if question := openwa_service.parse_question(payload):  # after yes/no, pdf and figures: anything left is a question
            return _handle_question(db, question)
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
