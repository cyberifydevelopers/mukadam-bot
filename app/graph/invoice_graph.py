"""Per-email invoice pipeline as a LangGraph graph:

    read_documents → analyze_documents ─(no bank document)─→ END  (rejected)
                            │ bank document(s)
                            ▼
                         notify → END                         (selected)

- read_documents: opens each PDF / Excel / CSV attachment (tries the
  configured bank passwords on locked PDFs); a PDF with no text is marked
  scanned, for the vision model.
- analyze_documents: per attachment, the LLM reads the DOCUMENT ITSELF —
  text, or the page images of a scanned PDF — and in one call decides
  whether it's a bank document, which bank, and extracts reference / date /
  amount / from / to / items. The email's subject and body are not used: a
  forwarded email's wording says nothing reliable about the attachment. A
  bank account statement is skipped — only invoices / bank documents are
  tracked.
- notify: one WhatsApp YES/NO message per selected document.

The database session travels in the run config (not the state), since the
graph has no checkpointer and nodes only need it to write records. Each
node records its stage on the email (set_stage) so the dashboard shows the
progress live.
"""

import json
import logging
from typing import TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import Session

from app.models import InvoiceRecord, InvoiceStatus, ProcessedEmail
from app.pipeline import create_record, extract_document, send_prompt, set_stage
from app.services.document_reader import Document, read_document
from app.services.gmail_service import FetchedEmail

logger = logging.getLogger(__name__)


class InvoiceState(TypedDict, total=False):
    email: FetchedEmail
    email_row_id: int
    documents: list[Document]
    selected_ids: list[int]  # InvoiceRecord ids to send
    prompts_sent: int


def _db(config: RunnableConfig) -> Session:
    return config["configurable"]["db"]


def _row(state: InvoiceState, config: RunnableConfig) -> ProcessedEmail:
    return _db(config).get(ProcessedEmail, state["email_row_id"])


_KIND_LABEL = {
    "pdf_text": "PDF",
    "pdf_scanned": "scanned PDF",
    "excel": "Excel",
    "csv": "CSV",
    "word": "Word",
    "email": "email body",
    "unreadable": "unreadable file",
}


def read_documents(state: InvoiceState, config: RunnableConfig) -> InvoiceState:
    docs = [read_document(p) for p in state["email"].attachment_paths]
    scanned = any(d.kind == "pdf_scanned" for d in docs)
    summary = ", ".join(f"{d.filename} ({_KIND_LABEL.get(d.kind, d.kind)})" for d in docs)
    set_stage(
        _db(config),
        _row(state, config),
        "analyzing",
        ("Scanning the PDF with the LLM (vision)" if scanned else "Reading the document with the LLM")
        + f" — is it a bank document? · {summary}",
    )
    return {"documents": docs}


def analyze_documents(state: InvoiceState, config: RunnableConfig) -> InvoiceState:
    db = _db(config)
    row = _row(state, config)
    selected: list[InvoiceRecord] = []
    reasons: list[str] = []
    outcomes: list[str] = []
    documents = list(state["documents"])

    def analyze(doc: Document, labelled: bool) -> None:
        record = create_record(db, row, doc)
        outcome = extract_document(db, record, doc)
        outcomes.append(outcome)
        extra = json.loads(record.raw_extracted_json or "{}")
        reason = extra.get("classification_reason") or (
            f"Couldn't read it: {record.parse_error}" if outcome == "failed" else ""
        )
        if outcome == "statement":
            reason = "Bank account statement — not tracked (only invoices / bank documents are)"
        reasons.append(f"{doc.filename}: {reason}" if labelled else reason)
        if outcome == "invoice":
            selected.append(record)

    # With attachments, only they decide: an email whose attachments aren't
    # bank documents is rejected, even when a bank officer forwarded them
    # (e.g. a customer's own request letter with "please arrange as per
    # attached request" from the bank). The body is read only when there's
    # no attachment at all (pipeline._documents_to_read()).
    for doc in documents:
        analyze(doc, labelled=len(documents) > 1)

    banks = [r.bank_name for r in selected if r.bank_name]
    row.bank_name = banks[0] if banks else None
    row.classification_reason = " | ".join(r for r in reasons if r)
    # Every attachment unreadable → failed (retryable); otherwise, with no
    # bank document among them → rejected.
    failed = not selected and all(o == "failed" for o in outcomes)
    if selected:
        row.classification = "bank"
        set_stage(db, row, "sending", f"Selected — sending {len(selected)} WhatsApp message(s)")
    elif failed:
        row.classification = None
        set_stage(db, row, "failed", row.classification_reason)
    else:
        row.classification = "not_bank"
        set_stage(db, row, "not_bank", row.classification_reason)
    logger.info("Email %r → %s: %s", row.subject, row.classification or "failed", row.classification_reason)
    return {"selected_ids": [r.id for r in selected]}


def route_after_analyze(state: InvoiceState) -> str:
    return "notify" if state["selected_ids"] else END


def notify(state: InvoiceState, config: RunnableConfig) -> InvoiceState:
    db = _db(config)
    sent = 0
    for record_id in state["selected_ids"]:
        record = db.get(InvoiceRecord, record_id)
        send_prompt(db, record)
        sent += record.status != InvoiceStatus.SEND_FAILED
    row = _row(state, config)
    set_stage(db, row, "done", f"WhatsApp sent for {sent} document(s)" if sent else "WhatsApp send failed")
    return {"prompts_sent": sent}


def _build():
    graph = StateGraph(InvoiceState)
    graph.add_node("read_documents", read_documents)
    graph.add_node("analyze_documents", analyze_documents)
    graph.add_node("notify", notify)
    graph.add_edge(START, "read_documents")
    graph.add_edge("read_documents", "analyze_documents")
    graph.add_conditional_edges("analyze_documents", route_after_analyze, ["notify", END])
    graph.add_edge("notify", END)
    return graph.compile()


invoice_graph = _build()


def run_invoice_graph(db: Session, email_row: ProcessedEmail, email: FetchedEmail) -> int:
    """Runs one fetched email (already recorded as `email_row`) through the
    graph. Returns the number of WhatsApp prompts sent."""
    final = invoice_graph.invoke(
        {"email": email, "email_row_id": email_row.id}, config={"configurable": {"db": db}}
    )
    return final.get("prompts_sent", 0)
