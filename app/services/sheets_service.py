"""Writes confirmed invoices to Google Sheets: one spreadsheet
(GOOGLE_SHEETS_SPREADSHEET_ID), one tab per bank, tab name = bank_code.
Confirmed statement transactions go to a separate "<bank_code> Transactions"
tab, since their columns differ. Replaces the local openpyxl-per-bank-file
approach.
"""

import json
import logging
from datetime import datetime

from googleapiclient.errors import HttpError

from app.config import settings
from app.formatting import quantity_total
from app.models import InvoiceRecord, StatementTransaction
from app.verification import check_lines
from app.services.google_auth import GOOGLE_API_RETRIES, sheets_client

logger = logging.getLogger(__name__)

HEADERS = [
    "Invoice ID",
    "Transaction Date",
    "Amount",
    "From (Sender)",
    "To (Receiver)",
    "Reference Number",
    "Source Email Sender",
    "PDF Filename",
    "Confirmed At",
    # Added with LLM bank classification — appended at the end so tabs
    # created before keep lining up.
    "Bank",
    "Currency",
    "From Account",
    "To Account",
    "Purpose",
    "Document Type",
    "Items",
    # Sender / our-company checks (app/verification.py), one per line.
    "Verification",
]


def _items_text(table: dict | None) -> str | None:
    """A document's line-items table in one cell: one row per line,
    "column: value" pairs (e.g. a delivery order's stock lines)."""
    if not table or not table.get("rows"):
        return None
    columns = table.get("columns") or []
    lines = []
    for row in table["rows"]:
        pairs = [
            f"{columns[i]}: {v}" if i < len(columns) and columns[i] else str(v)
            for i, v in enumerate(row)
            if str(v).strip()
        ]
        lines.append(" | ".join(pairs))
    return "\n".join(lines)

TRANSACTION_HEADERS = [
    "Txn ID",
    "Date",
    "Type",
    "Debit",
    "Credit",
    "Balance",
    "Party",
    "Reference",
    "Details",
    "Statement PDF",
    "Confirmed At",
]


class SheetsError(Exception):
    pass


def _ensure_tab(service, spreadsheet_id: str, tab_name: str, headers: list[str] = HEADERS) -> None:
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute(
        num_retries=GOOGLE_API_RETRIES
    )
    existing_titles = {s["properties"]["title"] for s in meta.get("sheets", [])}
    if tab_name in existing_titles:
        # A tab created before a column was added: extend its header row so
        # the new column is labelled.
        current = (
            service.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=f"'{tab_name}'!1:1")
            .execute(num_retries=GOOGLE_API_RETRIES).get("values", [[]])
        )
        if (current[0] if current else []) != headers:
            service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"'{tab_name}'!A1",
                valueInputOption="RAW",
                body={"values": [headers]},
            ).execute(num_retries=GOOGLE_API_RETRIES)
        return

    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]},
    ).execute(num_retries=GOOGLE_API_RETRIES)

    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab_name}'!A1",
        valueInputOption="RAW",
        body={"values": [headers]},
    ).execute(num_retries=GOOGLE_API_RETRIES)


def append_confirmed_invoice(invoice: InvoiceRecord) -> None:
    """Appends one confirmed invoice as a row in that bank's tab, creating
    the tab (with header row) the first time that bank is seen.

    No local file locking is needed here (unlike the openpyxl approach this
    replaces) — the Sheets API `values.append` call is a single atomic
    server-side request, so two confirmations landing at the same moment
    each get their own row without corrupting anything.
    """
    if not settings.google_sheets_spreadsheet_id:
        raise SheetsError("GOOGLE_SHEETS_SPREADSHEET_ID not configured")

    service = sheets_client()
    tab_name = invoice.bank_code
    extra = json.loads(invoice.raw_extracted_json or "{}")

    try:
        _ensure_tab(service, settings.google_sheets_spreadsheet_id, tab_name)

        row = [
            invoice.id,
            invoice.txn_date,
            # No money amount (e.g. a delivery order): the quantity total instead.
            invoice.amount or ((quantity_total(extra.get("items_table")) or (None, None))[1]),
            invoice.sender_name,
            invoice.receiver_name,
            invoice.reference_number,
            invoice.email.sender if invoice.email else None,
            invoice.pdf_filename,
            (invoice.confirmed_at or datetime.utcnow()).isoformat(),
            invoice.bank_name,
            extra.get("currency"),
            extra.get("sender_account"),
            extra.get("receiver_account"),
            extra.get("purpose"),
            extra.get("document_type"),
            _items_text(extra.get("items_table")),
            "\n".join(check_lines(extra)) or None,
        ]

        service.spreadsheets().values().append(
            spreadsheetId=settings.google_sheets_spreadsheet_id,
            range=f"'{tab_name}'!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()
    except HttpError as exc:
        raise SheetsError(f"Google Sheets API error: {exc}") from exc


def append_confirmed_transactions(txns: list[StatementTransaction]) -> None:
    """Appends confirmed statement transactions (one row each) to the bank's
    "<code> Transactions" tab in a single request, creating the tab (with
    header row) on first use. One call for the whole statement keeps the
    rows together and in order."""
    if not txns:
        return
    if not settings.google_sheets_spreadsheet_id:
        raise SheetsError("GOOGLE_SHEETS_SPREADSHEET_ID not configured")

    service = sheets_client()
    tab_name = f"{txns[0].bank_code} Transactions"

    try:
        _ensure_tab(service, settings.google_sheets_spreadsheet_id, tab_name, TRANSACTION_HEADERS)

        rows = [
            [
                txn.label,
                txn.txn_date,
                "Credit" if txn.credit else "Debit",
                txn.debit,
                txn.credit,
                txn.balance,
                txn.party,
                txn.reference,
                txn.details,
                txn.invoice.pdf_filename,
                (txn.confirmed_at or datetime.utcnow()).isoformat(),
            ]
            for txn in txns
        ]

        # Not retried: a retry after a lost response would append the rows twice.
        service.spreadsheets().values().append(
            spreadsheetId=settings.google_sheets_spreadsheet_id,
            range=f"'{tab_name}'!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        ).execute()
    except HttpError as exc:
        raise SheetsError(f"Google Sheets API error: {exc}") from exc
