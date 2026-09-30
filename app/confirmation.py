"""Applies an approver's Yes/No answer to whatever a WhatsApp prompt was
about — a single-payment PDF (InvoiceRecord), a whole account statement
(an InvoiceRecord with transactions: one answer covers all its rows), or a
single statement row (StatementTransaction, from the earlier per-row
prompts). Shared by the Meta
(app/routers/whatsapp_webhook.py) and OpenWA (app/routers/openwa_webhook.py)
webhooks, which differ only in how the answer arrives.
"""

import json
import re
from datetime import datetime

from sqlalchemy.orm import Session

from app.formatting import amount_line
from app.models import InvoiceRecord, InvoiceStatus, StatementTransaction
from app.services.sheets_service import append_confirmed_invoice, append_confirmed_transactions

Confirmable = InvoiceRecord | StatementTransaction


def find_by_message_id(db: Session, message_id: str) -> Confirmable | None:
    """The invoice or transaction whose prompt had this WhatsApp message id."""
    for model in (StatementTransaction, InvoiceRecord):
        target = db.query(model).filter(model.whatsapp_message_id == message_id).first()
        if target is not None:
            return target
    return None


def find_by_label(db: Session, label: str) -> Confirmable | None:
    """"T12" → statement transaction 12, "7" → invoice 7 (as printed in prompts)."""
    if label.upper().startswith("T"):
        return db.get(StatementTransaction, int(label[1:]))
    return db.get(InvoiceRecord, int(label))


def pending_for_number(db: Session, phone_digits: str) -> list[Confirmable]:
    """Everything still waiting on an answer from this number, oldest first."""
    pending: list[Confirmable] = []
    for model in (InvoiceRecord, StatementTransaction):
        rows = (
            db.query(model)
            .filter(model.status == InvoiceStatus.PENDING_CONFIRMATION)
            .order_by(model.created_at, model.id)
            .all()
        )
        pending += [r for r in rows if re.sub(r"\D", "", r.whatsapp_to_number or "") == phone_digits]
    return pending


def _saved_invoice_text(record: InvoiceRecord) -> str:
    extra = json.loads(record.raw_extracted_json or "{}")
    lines = [f"Saved to the {record.bank_code} sheet:", extra.get("document_type") or "Invoice"]
    if record.reference_number:
        lines.append(f"Reference:  {record.reference_number}")
    if record.txn_date:
        lines.append(f"Date:  {record.txn_date}")
    if amount := amount_line(record):
        lines.append(f"{amount[0]}:  {amount[1]}")
    return "\n".join(lines)


def label_of(target: Confirmable) -> str:
    return target.label if isinstance(target, StatementTransaction) else f"#{target.id}"


def apply_answer(db: Session, target: Confirmable, answer: str) -> str | None:
    """`answer` is "yes" or "no" (case-insensitive). Returns the
    acknowledgement text, or None if the answer wasn't recognized or the
    item is no longer pending (e.g. a retried webhook delivery, or a second
    reaction on the same prompt — idempotent no-op)."""
    if target.status != InvoiceStatus.PENDING_CONFIRMATION:
        return None

    answer = answer.strip().lower()
    if answer not in ("yes", "no"):
        return None
    now = datetime.utcnow()

    if isinstance(target, InvoiceRecord) and target.transactions:
        # A statement: the answer covers every row not already answered
        # individually.
        rows = [t for t in target.transactions if t.status == InvoiceStatus.PENDING_CONFIRMATION]
        for row in rows:
            row.status = InvoiceStatus.CONFIRMED if answer == "yes" else InvoiceStatus.REJECTED
            row.confirmed_at = now if answer == "yes" else None
        target.status = InvoiceStatus.CONFIRMED if answer == "yes" else InvoiceStatus.REJECTED
        target.confirmed_at = now if answer == "yes" else None
        db.flush()
        if answer == "yes":
            append_confirmed_transactions(rows)
            ack = f"Saved: {len(rows)} transactions of statement #{target.id} added to the \"{target.bank_code} Transactions\" sheet."
        else:
            ack = f"Skipped: statement #{target.id} ({len(rows)} transactions) was not saved."
    elif answer == "yes":
        target.status = InvoiceStatus.CONFIRMED
        target.confirmed_at = now
        db.flush()
        if isinstance(target, StatementTransaction):
            append_confirmed_transactions([target])
            ack = f"Saved {target.label} to the {target.bank_code} Transactions sheet."
        else:
            append_confirmed_invoice(target)
            ack = _saved_invoice_text(target)
    else:
        target.status = InvoiceStatus.REJECTED
        ack = f"Skipped: {label_of(target)} was not saved."

    db.add(target)
    return ack
