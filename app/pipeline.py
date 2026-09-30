"""Orchestrates ingestion: turn 'the mailbox changed' (a Pub/Sub
notification, or the fallback poll) into extracted invoices / bank documents
sent for WhatsApp confirmation. Per email, the steps (read attachments →
LLM classifies bank or not → extract → notify) run as a LangGraph graph
(app/graph/invoice_graph.py); this module holds the Gmail sync loop and the
extract / send building blocks the graph calls. The confirmation half
(webhook -> Sheets write) lives in app/confirmation.py and the webhook
routers.
"""

import json
import logging
import os
import re
import threading
from datetime import datetime

from googleapiclient.errors import HttpError
from sqlalchemy.orm import Session, object_session

from app.bank_registry import BANK_EMAIL_DOMAINS, BANK_REGISTRY, BankProfile, bank_identity, profile_by_code
from app.config import settings
from app.formatting import amount_line, format_items
from app.models import GmailSyncState, InvoiceRecord, InvoiceStatus, ProcessedEmail, StatementTransaction
from app.parsers.base import ExtractedInvoice
from app.parsers.llm_parser import LlmInvoiceParser, LlmParseError
from app.parsers.statement_rows import parse_statement
from app.services import gmail_service, openwa_service, whatsapp_service
from app.services.document_reader import Document, read_document
from app.services.message_log import log_message
from app.services.pdf_extractor import PdfExtractionError
from app.verification import check_lines, verify

logger = logging.getLogger(__name__)

# Shared across all banks that don't have their own hand-written parser —
# LLM-first, rule-based override (see app/bank_registry.py docstring). One
# instance so its OpenRouter client is reused rather than rebuilt per PDF.
_DEFAULT_LLM_PARSER = LlmInvoiceParser()

# Syncs are triggered from several threads (Pub/Sub listener, the fallback
# poll, the dashboard button); two at once would both see the same new
# message and process it twice.
_SYNC_LOCK = threading.Lock()


def _pdf_password_for(profile: BankProfile) -> str | None:
    if not profile.pdf_password_env:
        return None
    return os.environ.get(profile.pdf_password_env)


def _get_or_init_sync_state(db: Session) -> GmailSyncState:
    state = db.get(GmailSyncState, 1)
    if state is None:
        history_id, expiration_ms = gmail_service.start_watch()
        state = GmailSyncState(id=1, last_history_id=history_id, watch_expiration_ms=expiration_ms)
        db.add(state)
        db.flush()
        logger.info("Initialized Gmail watch, baseline historyId=%s", history_id)
    return state


def sync_gmail(db: Session, notification: tuple[datetime, datetime] | None = None) -> int:
    """Pulls everything new since the last known Gmail historyId and runs
    each email through the invoice graph. Triggered by the Pub/Sub listener
    on every new-mail notification (or the fallback poll / dashboard
    button); works from its own stored cursor, so a missed or duplicated
    notification never skips or double-processes mail.
    """
    with _SYNC_LOCK:
        return _sync_gmail_locked(db, notification)


def _sync_gmail_locked(db: Session, notification: tuple[datetime, datetime] | None) -> int:
    state = _get_or_init_sync_state(db)

    try:
        message_ids, new_history_id = gmail_service.list_new_message_ids(state.last_history_id)
    except HttpError as exc:
        # Gmail rejects a startHistoryId once it falls outside its retention
        # window (a few days) with a 404 — re-baseline from a fresh watch
        # rather than get stuck retrying a call that can never succeed. Any
        # other failure (network blip, 5xx) propagates and the cursor is left
        # alone, so the next notification/poll retries without losing mail.
        if exc.resp.status != 404:
            raise
        logger.error("history.list failed (%s) — re-baselining Gmail watch", exc)
        history_id, expiration_ms = gmail_service.start_watch()
        state.last_history_id = history_id
        state.watch_expiration_ms = expiration_ms
        db.commit()
        return 0

    already_seen = {row[0] for row in db.query(ProcessedEmail.message_id).all()}
    created_count = 0

    for message_id in message_ids:
        if message_id in already_seen:
            continue
        created_count += _process_one_message(db, message_id, notification)

    state.last_history_id = new_history_id
    db.commit()
    return created_count


def set_stage(db: Session, email: ProcessedEmail, stage: str, detail: str | None = None) -> None:
    """Records where an email is in processing and COMMITS right away, so the
    dashboard (a separate connection) shows the step while it's running."""
    email.stage = stage
    email.stage_detail = detail
    email.stage_updated_at = datetime.utcnow()
    db.commit()


def _mentions_bank(fetched: gmail_service.FetchedEmail) -> bool:
    """Cheap pre-check before spending an LLM call on an email's body: a
    bank's own email names it somewhere, even after several forwards — the
    word "bank" in its domain, signature or disclaimer ("Meezan Bank
    Limited", @meezanbank.com), or a known bank domain without it
    (@habibmetro.com — BANK_EMAIL_DOMAINS)."""
    text = f"{fetched.sender}\n{fetched.subject}\n{fetched.body_text}".lower()
    return (
        "bank" in text
        or any("@" + domain in text for domain in BANK_EMAIL_DOMAINS)
        or any(p.display_name.lower() in text or p.bank_code.lower() in text for p in BANK_REGISTRY)
    )


def _may_be_from_bank(fetched: gmail_service.FetchedEmail) -> bool:
    """Whether the email (text + pictures) gets an LLM look. With
    SCAN_ALL_EMAILS (the default) every email does — subject, text, logos and
    pictures are all checked. Otherwise only when its text names a bank, or
    it has pictures (a logo or a photographed document can be the only sign
    of the bank) and isn't newsletter / mailing-list mail."""
    if settings.scan_all_emails:
        return True
    return _mentions_bank(fetched) or bool(fetched.image_paths and not fetched.is_bulk)


def _documents_to_read(fetched: gmail_service.FetchedEmail) -> str | None:
    """Decides what the graph reads for this email. Attachments when it has
    any; otherwise the email itself (text + pictures) — many bank
    instructions arrive as plain (often forwarded) emails with the details in
    the body — saved as a text document and added to
    `fetched.attachment_paths`. Returns why the email is skipped instead, or
    None."""
    if fetched.attachment_paths:
        return None
    if not fetched.body_text and not fetched.image_paths:
        return "No attachment and an empty body — nothing to read"
    if not _may_be_from_bank(fetched):
        return "No attachment, no bank named and no pictures to check — skipped without an LLM call"
    fetched.attachment_paths = [gmail_service.save_body_document(fetched)]
    return None


def _is_bank(extracted: ExtractedInvoice) -> bool:
    """Only a document ISSUED BY a bank (its letterhead) counts — a
    customer's letter addressed to a bank is not one, even if the LLM's
    overall flag says otherwise."""
    return extracted.extra.get("is_bank_document") is not False and extracted.extra.get("issuer_is_bank") is not False


def _process_one_message(db: Session, message_id: str, notification: tuple[datetime, datetime] | None = None) -> int:
    """`notification` = (published_at, received_at) of the Pub/Sub message
    that triggered this sync, kept on the email for the latency breakdown."""
    try:
        fetched = gmail_service.fetch_email(message_id)
    except HttpError as exc:
        # Deleted (or a draft discarded) between arriving and being fetched —
        # history still lists it, but it's gone. Skip it; raising would stop
        # the sync before the cursor moves, so every later sync would hit
        # the same message and no new mail would ever get through.
        if exc.resp.status != 404:
            raise
        logger.warning("Message %s no longer exists in Gmail — skipped", message_id)
        return 0
    skip_reason = _documents_to_read(fetched)
    names =[os.path.basename(p).split("_", 1)[-1] for p in fetched.attachment_paths]
    email = ProcessedEmail(
        message_id=fetched.message_id,
        sender=fetched.sender,
        subject=fetched.subject,
        received_at=fetched.received_at,
        attachment_names=json.dumps(names),
        chain_senders=json.dumps(gmail_service.chain_senders(fetched)),
        notification_published_at=notification[0] if notification else None,
        notification_received_at=notification[1] if notification else None,
    )
    db.add(email)
    if skip_reason:
        set_stage(db, email, "no_attachment", skip_reason)
        return 0
    set_stage(db, email, "reading", f"Opening {', '.join(names)}")
    return _run_graph(db, email, fetched)


def _run_graph(db: Session, email: ProcessedEmail, fetched: gmail_service.FetchedEmail) -> int:
    from app.graph.invoice_graph import run_invoice_graph  # graph → pipeline import cycle

    try:
        return run_invoice_graph(db, email, fetched)
    except Exception as exc:
        # One bad email must not stop the sync (the cursor would never move
        # past it). It's marked failed — with the reason — for a retry from
        # the dashboard.
        logger.exception("Processing email %s failed", fetched.message_id)
        db.rollback()
        email = db.merge(email)
        set_stage(db, email, "failed", f"{type(exc).__name__}: {exc}")
        return 0


def retry_email(db: Session, email: ProcessedEmail) -> int:
    """Runs a failed email through the graph again: re-downloads it from
    Gmail (its attachments may never have been saved) and drops whatever
    partial records the failed run left."""
    with _SYNC_LOCK:
        for record in list(email.invoices):
            for txn in record.transactions:
                db.delete(txn)
            db.delete(record)
        email.classification = email.bank_name = email.classification_reason = None
        set_stage(db, email, "reading", "Retrying — downloading the email again")
        fetched = gmail_service.fetch_email(email.message_id)
        skip_reason = _documents_to_read(fetched)
        email.attachment_names = json.dumps([os.path.basename(p).split("_", 1)[-1] for p in fetched.attachment_paths])
        email.chain_senders = json.dumps(gmail_service.chain_senders(fetched))
        if skip_reason:
            set_stage(db, email, "no_attachment", skip_reason)
            return 0
        created = _run_graph(db, email, fetched)
        db.commit()
        return created


def create_record(db: Session, email: ProcessedEmail, doc: Document) -> InvoiceRecord:
    record = InvoiceRecord(email=email, pdf_filename=os.path.basename(doc.path), bank_code="UNKNOWN")
    db.add(record)
    return record


def _set_bank(record: InvoiceRecord, bank_name: str | None) -> None:
    if bank_name:
        record.bank_code, record.bank_name = bank_identity(bank_name, None)


def _bank_in_text(text: str) -> str | None:
    """A registered bank named in a statement's header — saves an LLM call
    for the (text) statements of banks we already know."""
    head = text[:600].lower()
    for profile in BANK_REGISTRY:
        if profile.display_name.lower() in head:
            return profile.display_name
    return None


def extract_document(db: Session, record: InvoiceRecord, doc: Document | None = None) -> str:
    """Reads one attachment into `record` and decides — from the document
    itself, never the email's wording — whether it's a bank document.
    Returns the outcome: "invoice" (ready to send, status
    PENDING_CONFIRMATION), "not_bank", "statement" (an account statement —
    not tracked, nothing sent) or "failed".

    - An account statement (text PDF whose rows reconcile against the
      running balance — app/parsers/statement_rows.py) is skipped: only
      invoices / bank documents are tracked.
    - A text PDF, Excel or CSV goes to the bank's parser (default: the LLM)
      as text; a scanned PDF (no text layer) to the LLM's vision input. The
      same call says whether it's a bank document, which bank, and why.
    - An email with no attachment (its body saved as a document) goes to the
      LLM too, judged by who wrote the original message of the forward chain.
    """
    doc = doc or read_document(os.path.join(settings.pdf_storage_dir, record.pdf_filename))
    if doc.kind == "unreadable":
        _mark_failed(record, doc.path, PdfExtractionError(doc.error or "unreadable file"))
        return "failed"

    # Only invoices / bank documents are tracked — an account statement is
    # recognised (its rows reconcile with the running balance) and skipped
    # without an LLM call.
    if doc.kind == "pdf_text" and parse_statement(doc.text):
        _set_bank(record, _bank_in_text(doc.text))
        return _skip_statement(record)

    try:
        if doc.kind == "pdf_scanned":
            extracted: ExtractedInvoice = _DEFAULT_LLM_PARSER.parse_from_pdf_bytes(doc.pdf_bytes)
        elif doc.kind == "email":
            extracted = _DEFAULT_LLM_PARSER.parse_email(doc.text, doc.image_paths)
            if not extracted.extra.get("bank_name"):
                # An email counts only when the bank itself can be named
                # (domain, signature, logo) — bank-sounding wording isn't enough.
                extracted.extra["is_bank_document"] = False
        else:
            extracted = _DEFAULT_LLM_PARSER.parse(doc.text, doc.image_paths)
            if doc.kind == "pdf_text" and not _is_bank(extracted):
                # The text layer has no pictures: a bank named only by the
                # logo on its letterhead looks like "not a bank" from the
                # text. Look at the pages themselves before rejecting it.
                seen = _DEFAULT_LLM_PARSER.parse_from_pdf_bytes(doc.pdf_bytes)
                if _is_bank(seen):
                    logger.info("%s: the page images show a bank document the text alone didn't", record.pdf_filename)
                    extracted = seen
        _set_bank(record, extracted.extra.get("bank_name"))
        # A bank with a hand-written parser: its fields replace the LLM's
        # (the LLM still decided bank / not bank and which bank).
        profile = profile_by_code(record.bank_code)
        if doc.kind == "pdf_text" and profile.parser is not None:
            extracted = profile.parser.parse(doc.text)
    except (PdfExtractionError, LlmParseError) as exc:
        _mark_failed(record, doc.path, exc)
        return "failed"

    record.txn_date = extracted.txn_date
    record.amount = extracted.amount
    record.sender_name = extracted.sender_name
    record.receiver_name = extracted.receiver_name
    record.reference_number = extracted.reference_number
    record.raw_extracted_json = json.dumps({**extracted.extra, "source_kind": doc.kind})
    record.parse_error = None

    if extracted.extra.get("is_account_statement"):
        return _skip_statement(record)

    if not _is_bank(extracted):
        record.status = InvoiceStatus.NOT_BANK
        logger.info("%s is not a bank document — nothing sent", record.pdf_filename)
        return "not_bank"

    # The letterhead said bank; now verify it against the email it came in
    # (did that bank send it? is it addressed to us?). Shown as ✓ / ⚠ on the
    # prompt — a failed check warns the approver, it doesn't reject.
    checks = verify(record, extracted, doc.text)
    record.raw_extracted_json = json.dumps({**json.loads(record.raw_extracted_json), "checks": checks})
    record.status = InvoiceStatus.PENDING_CONFIRMATION
    return "invoice"


def send_prompt(db: Session, record: InvoiceRecord) -> None:
    """Sends the WhatsApp YES/NO message for an extracted document."""
    db.flush()  # ids for the message labels
    if record.transactions:
        _send_statement(record)
    else:
        _send_confirmation(record)


def _extract_and_send(db: Session, record: InvoiceRecord, doc: Document | None = None) -> int:
    """Extract + send in one go (dashboard re-parse). Returns 1 if a prompt was sent."""
    if extract_document(db, record, doc) == "invoice":
        send_prompt(db, record)
        return 1
    return 0


def _mark_failed(record: InvoiceRecord, pdf_path: str, exc: Exception) -> None:
    record.status = InvoiceStatus.FAILED_PARSE
    record.parse_error = str(exc)
    logger.error("Failed to parse %s: %s", pdf_path, exc)


def _skip_statement(record: InvoiceRecord) -> str:
    """An account statement: not tracked (the bot follows invoices / bank
    documents, not every account transaction) — no rows, no WhatsApp."""
    record.status = InvoiceStatus.STATEMENT
    record.parse_error = None
    record.raw_extracted_json = json.dumps(
        {"classification_reason": "Bank account statement — not tracked (only invoices / bank documents are)"}
    )
    logger.info("%s is an account statement — skipped", record.pdf_filename)
    return "statement"


def reparse_invoice(db: Session, record: InvoiceRecord) -> None:
    """Re-runs extraction on an already-downloaded PDF (e.g. after the LLM
    call failed on a network error, or to split a statement that was read
    before statement support existed) and starts its WhatsApp confirmation."""
    _extract_and_send(db, record)
    db.commit()


def resend_confirmation(db: Session, target: InvoiceRecord | StatementTransaction) -> None:
    """Re-sends the WhatsApp prompt for something that failed to send or is
    still waiting — e.g. after fixing the WhatsApp config (dashboard button)."""
    target.status = InvoiceStatus.PENDING_CONFIRMATION
    if isinstance(target, StatementTransaction):
        # Rows are confirmed as part of their statement now.
        target.invoice.status = InvoiceStatus.PENDING_CONFIRMATION
        _send_statement(target.invoice)
    elif target.transactions:
        _send_statement(target)
    else:
        _send_confirmation(target)
    db.commit()


def display_bank_name(record: InvoiceRecord) -> str:
    return record.bank_name or profile_by_code(record.bank_code).display_name


def _money(value: str | None) -> str:
    return f"Rs {float(value):,.2f}" if value else "N/A"


def _short_amount(value: str) -> str:
    """"100000.00" → "100,000"; keeps paisa only when there are some."""
    n = float(value)
    return f"{n:,.0f}" if n == int(n) else f"{n:,.2f}"


def _row_party(txn: StatementTransaction) -> str:
    # No named counterparty (transfers, ATM, bill payments): use the row's
    # own description ("TRANSFER CREDIT", "VDC Pak - W/D", "UBPS Pymt").
    return txn.party or txn.details.split(",")[0].strip()


# WhatsApp caps a message at 4096 characters; stay under it with room for
# the header/footer, splitting a long statement's table across messages.
_TABLE_CHUNK_CHARS = 3300


def _send_statement(record: InvoiceRecord) -> None:
    """One WhatsApp message for the whole statement: totals, then a compact
    monospace table (date, +/-amount, party) of the rows awaiting an answer,
    then the single YES/NO question. A very long statement's table is split
    across a few messages; the question is always in the last one, and
    that's the message the answer is matched to."""
    bank_name = display_bank_name(record)
    rows = [t for t in record.transactions if t.status == InvoiceStatus.PENDING_CONFIRMATION]
    extra = json.loads(record.raw_extracted_json or "{}")
    credits = [t for t in rows if t.credit]
    debits = [t for t in rows if t.debit]
    credit_total = sum(float(t.credit) for t in credits)
    debit_total = sum(float(t.debit) for t in debits)
    pdf_name = record.pdf_filename.split("_", 1)[-1]

    header = [
        f"*{bank_name.upper()}*",
        f"Account statement  (#{record.id})",
        "",
        f"File:  {pdf_name}",
        f"Period:  {record.txn_date}",
        f"Transactions:  {len(rows)}",
        f"Credits:  {len(credits)}  |  {_money(f'{credit_total:.2f}')}",
        f"Debits:  {len(debits)}  |  {_money(f'{debit_total:.2f}')}",
    ]
    if extra.get("opening") is not None and extra.get("closing") is not None:
        header.append(f"Opening:  {_money(str(extra['opening']))}")
        header.append(f"Closing:  {_money(str(extra['closing']))}")
    if extra.get("duplicates_skipped"):
        header.append(f"({extra['duplicates_skipped']} rows were already sent from an earlier statement and are left out)")
    if any(not t.reconciled for t in rows):
        header.append("Note: some rows don't match the running balance - please check the PDF.")

    table_lines = [
        f"{t.txn_date[:5]} {('+' if t.credit else '-') + _short_amount(t.credit or t.debit):>10} {_row_party(t)[:18]}"
        for t in rows
    ]
    chunks: list[list[str]] = [[]]
    for line in table_lines:
        if sum(len(l) + 1 for l in chunks[-1]) + len(line) > _TABLE_CHUNK_CHARS:
            chunks.append([])
        chunks[-1].append(line)

    footer = [
        "",
        f"*Save all {len(rows)} transactions to the Google Sheet?*",
        "Reply YES to save, or NO to skip.",
        f"(With several waiting, reply YES {record.id} or NO {record.id}.)",
    ]

    messages = []
    for i, chunk in enumerate(chunks):
        part = f" ({i + 1}/{len(chunks)})" if len(chunks) > 1 else ""
        body = ["DATE      AMOUNT PARTY" + part, *chunk]
        text = "\n".join(([*header, ""] if i == 0 else []) + ["```", *body, "```"])
        messages.append(text)
    messages[-1] += "\n" + "\n".join(footer)

    # Earlier parts are informational; the last (with the question) is the
    # prompt the answer is matched against.
    for text in messages[:-1]:
        _deliver(record, text, [], record_id=False)
    _deliver(
        record,
        messages[-1],
        [bank_name, f"{len(rows)} transactions", record.txn_date or "N/A", f"Statement #{record.id}"],
    )


def _send_confirmation(record: InvoiceRecord) -> None:
    """The YES/NO message for one document: its fields one per line, then
    its items (if it has a table), in plain text."""
    bank_display_name = display_bank_name(record)
    extra = json.loads(record.raw_extracted_json or "{}")
    amount = amount_line(record)
    amount_label, amount_value = amount or ("Amount", None)
    if amount_value and record.amount and extra.get("amount_sanity_check") == "mismatch":
        # LLM-extracted amount disagreed with an independent regex scan of
        # the same text.
        amount_value += "  (unverified - please check the PDF)"

    def party(name: str | None, account: str | None) -> str | None:
        return "  |  ".join(p for p in (name, account) if p) or None

    fields = [
        ("Reference", record.reference_number),
        ("Date", record.txn_date),
        (amount_label, amount_value),
        ("From", party(record.sender_name, extra.get("sender_account"))),
        ("To", party(record.receiver_name, extra.get("receiver_account"))),
        ("Purpose", extra.get("purpose")),
    ]
    table = extra.get("items_table") or {}
    items = format_items(table.get("columns") or [], table.get("rows") or [])

    lines = [
        f"*{bank_display_name.upper()}*",
        f"{extra.get('document_type') or 'Invoice'}  (#{record.id})",
        "",
    ]
    lines += [f"{label}:  {value}" for label, value in fields if value]
    missing = [label for label, value in fields[:4] if not value]
    if missing:
        lines.append(f"Not found in the document: {', '.join(missing)}")
    if checks := check_lines(extra):
        lines += ["", *checks]
    if items:
        lines += ["", f"*Items ({len(table['rows'])})*", "", *items]
    lines += [
        "",
        "*Save to the Google Sheet?*",
        "Reply YES to save, or NO to skip.",
        f"(With several waiting, reply YES {record.id} or NO {record.id}.)",
    ]
    text = "\n".join(lines)
    # Template body params — order must match the placeholders in the
    # approved template (see docs/WORKFLOW.md for the exact template text).
    template_params = [
        bank_display_name,
        amount_value or "N/A",
        record.txn_date or "N/A",
        record.reference_number or "N/A",
    ]
    _deliver(record, text, template_params)


def _deliver(
    target: InvoiceRecord | StatementTransaction,
    text: str,
    template_params: list[str],
    record_id: bool = True,
) -> None:
    """Sends a confirmation prompt through the configured provider, records
    its message id on `target` (to match the answer back to it), and logs
    it. On failure `target` becomes SEND_FAILED.

    record_id=False sends an informational message about `target` (e.g. the
    first part of a long statement table) without making it the prompt the
    answer is matched to. Meta only allows the approved template outside a
    24h session, so such extra messages are skipped there."""
    if not record_id and settings.whatsapp_provider != "openwa":
        return
    is_txn = isinstance(target, StatementTransaction)
    label = target.label if is_txn else f"invoice {target.id}"
    recipients = settings.notify_numbers_list
    provider = settings.whatsapp_provider
    to_number = recipients[0] if recipients else None
    message_id = error = None

    if not to_number:
        error = "NOTIFY_WHATSAPP_NUMBERS not configured"
    elif provider == "openwa":
        try:
            message_id = openwa_service.send_text(to_number, text)
        except openwa_service.OpenWaSendError as exc:
            error = str(exc)
    else:
        text = f"[template {settings.whatsapp_template_name}] " + " | ".join(template_params)
        try:
            message_id = whatsapp_service.send_confirmation_template(to_number, template_params)
        except whatsapp_service.WhatsAppSendError as exc:
            error = str(exc)

    if error:
        logger.error("WhatsApp (%s) send failed for %s: %s", provider, label, error)
        target.status = InvoiceStatus.SEND_FAILED
    elif record_id:
        target.whatsapp_message_id = message_id
        target.whatsapp_to_number = to_number

    log_message(
        object_session(target),
        direction="out",
        provider=provider,
        peer_number=to_number,
        text=text,
        status="failed" if error else "sent",
        message_id=message_id,
        invoice_id=target.invoice_id if is_txn else target.id,
        transaction_id=target.id if is_txn else None,
        error=error,
    )
