import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class InvoiceStatus(str, enum.Enum):
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    FAILED_PARSE = "FAILED_PARSE"
    UNKNOWN_BANK = "UNKNOWN_BANK"
    SEND_FAILED = "SEND_FAILED"
    # The document turned out not to be a bank document (the email looked
    # like it might be; the extractor said otherwise) — nothing is sent.
    NOT_BANK = "NOT_BANK"
    # A bank account statement — not tracked (only invoices / bank documents
    # are), so nothing is sent or saved.
    STATEMENT = "STATEMENT"


class GmailSyncState(Base):
    """Singleton row (id=1) holding the Gmail History API cursor and the
    current watch()'s expiration, so the bot resumes from the right place
    after a restart and knows when the watch needs renewing."""

    __tablename__ = "gmail_sync_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_history_id: Mapped[str] = mapped_column(String(64), nullable=True)
    watch_expiration_ms: Mapped[int] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class AppSetting(Base):
    """Settings changed from the dashboard, overriding the matching .env
    value (see app/runtime_settings.py). Kept in the db so they survive a
    restart."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class ProcessedEmail(Base):
    """One row per Gmail message we have already looked at, so a Pub/Sub
    push notification (or the periodic reconciliation sync) never
    re-processes the same message twice."""

    __tablename__ = "processed_emails"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    sender: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str] = mapped_column(String(998))
    received_at: Mapped[datetime] = mapped_column(DateTime)
    processed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # The LLM's verdict on the email (app/graph/invoice_graph.py):
    # "bank" / "not_bank" / "unsure", which bank, and why.
    classification: Mapped[str] = mapped_column(String(16), nullable=True)
    bank_name: Mapped[str] = mapped_column(String(128), nullable=True)
    classification_reason: Mapped[str] = mapped_column(Text, nullable=True)

    # Live processing progress, committed as each step starts so the
    # dashboard can show it while the email is still being handled:
    # reading → classifying → extracting → done, or no_attachment /
    # not_bank / failed.
    stage: Mapped[str] = mapped_column(String(16), nullable=True)
    stage_detail: Mapped[str] = mapped_column(Text, nullable=True)
    stage_updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    attachment_names: Mapped[str] = mapped_column(Text, nullable=True)  # JSON list, as received
    # JSON list of every sender address in the email's forward chain (its
    # From header + each forwarded "From:" line) — checked against the bank
    # on the document (app/verification.py).
    chain_senders: Mapped[str] = mapped_column(Text, nullable=True)
    # Latency breakdown for the dashboard: when Google published the Pub/Sub
    # notification that led to this email being fetched, and when this app
    # received that notification (received_at → notified_at is Gmail/Google's
    # delay; notified_at → processed_at is ours).
    notification_published_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    notification_received_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)

    invoices: Mapped[list["InvoiceRecord"]] = relationship(back_populates="email")


class InvoiceRecord(Base):
    """One row per PDF attachment extracted from an email, tracked through
    the WhatsApp confirmation loop up to the Google Sheets write."""

    __tablename__ = "invoice_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("processed_emails.id"))
    email: Mapped["ProcessedEmail"] = relationship(back_populates="invoices")

    pdf_filename: Mapped[str] = mapped_column(String(512))
    bank_code: Mapped[str] = mapped_column(String(64), default="UNKNOWN")  # also the Sheet tab name
    bank_name: Mapped[str] = mapped_column(String(128), nullable=True)  # as the LLM identified it

    # Extracted fields (kept both structured, for the Excel row, and as raw
    # JSON text so nothing is lost if a bank format has extra fields).
    txn_date: Mapped[str] = mapped_column(String(32), nullable=True)
    amount: Mapped[str] = mapped_column(String(32), nullable=True)
    sender_name: Mapped[str] = mapped_column(String(255), nullable=True)
    receiver_name: Mapped[str] = mapped_column(String(255), nullable=True)
    reference_number: Mapped[str] = mapped_column(String(128), nullable=True)
    raw_extracted_json: Mapped[str] = mapped_column(Text, nullable=True)
    parse_error: Mapped[str] = mapped_column(Text, nullable=True)

    status: Mapped[InvoiceStatus] = mapped_column(
        Enum(InvoiceStatus), default=InvoiceStatus.PENDING_CONFIRMATION
    )

    whatsapp_message_id: Mapped[str] = mapped_column(String(128), nullable=True)
    whatsapp_to_number: Mapped[str] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)

    transactions: Mapped[list["StatementTransaction"]] = relationship(
        back_populates="invoice", order_by="StatementTransaction.row_no"
    )


class StatementTransaction(Base):
    """One row of an account-statement PDF. The statement is sent to WhatsApp
    as one message (a table of its rows); confirming it appends every row to
    the bank's "<code> Transactions" sheet tab."""

    __tablename__ = "statement_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoice_records.id"), index=True)
    invoice: Mapped["InvoiceRecord"] = relationship(back_populates="transactions")
    row_no: Mapped[int] = mapped_column(Integer)  # 1-based position in the statement

    txn_date: Mapped[str] = mapped_column(String(32))
    details: Mapped[str] = mapped_column(Text)
    party: Mapped[str] = mapped_column(String(255), nullable=True)
    reference: Mapped[str] = mapped_column(String(128), nullable=True)
    # Kept as the statement's own decimal strings ("100000.00") — no float rounding.
    debit: Mapped[str] = mapped_column(String(32), nullable=True)
    credit: Mapped[str] = mapped_column(String(32), nullable=True)
    balance: Mapped[str] = mapped_column(String(32))
    reconciled: Mapped[bool] = mapped_column(Boolean, default=True)

    status: Mapped[InvoiceStatus] = mapped_column(
        Enum(InvoiceStatus), default=InvoiceStatus.PENDING_CONFIRMATION, index=True
    )
    whatsapp_message_id: Mapped[str] = mapped_column(String(255), nullable=True, index=True)
    whatsapp_to_number: Mapped[str] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)

    @property
    def label(self) -> str:
        """How the bot names it in messages ("T12"), distinct from invoice ids."""
        return f"T{self.id}"

    @property
    def bank_code(self) -> str:
        return self.invoice.bank_code


class WhatsAppMessage(Base):
    """Every WhatsApp message the bot sent or received — prompts, replies,
    acknowledgements — so the dashboard can show what actually went out and
    whether it was delivered/read (updated from OpenWA `message.ack`)."""

    __tablename__ = "whatsapp_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoice_records.id"), nullable=True, index=True)
    transaction_id: Mapped[int] = mapped_column(
        ForeignKey("statement_transactions.id"), nullable=True, index=True
    )
    direction: Mapped[str] = mapped_column(String(8))  # "out" / "in"
    provider: Mapped[str] = mapped_column(String(16))  # "openwa" / "meta"
    peer_number: Mapped[str] = mapped_column(String(64), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    message_id: Mapped[str] = mapped_column(String(255), nullable=True, index=True)
    # out: sent / delivered / read / failed — in: received
    status: Mapped[str] = mapped_column(String(16))
    error: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
