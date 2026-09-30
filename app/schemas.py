from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models import InvoiceStatus


class InvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    bank_code: str
    pdf_filename: str
    txn_date: str | None
    amount: str | None
    sender_name: str | None
    receiver_name: str | None
    reference_number: str | None
    status: InvoiceStatus
    created_at: datetime
    confirmed_at: datetime | None
