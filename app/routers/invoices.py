from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import InvoiceRecord
from app.pipeline import sync_gmail
from app.schemas import InvoiceOut

router = APIRouter(prefix="/invoices", tags=["invoices"])


@router.get("", response_model=list[InvoiceOut])
def list_invoices(db: Session = Depends(get_db)):
    return db.query(InvoiceRecord).order_by(InvoiceRecord.created_at.desc()).all()


@router.post("/poll-now")
def poll_now(db: Session = Depends(get_db)):
    """Manually triggers a Gmail sync instead of waiting for a Pub/Sub push
    or the next reconciliation tick — useful in development/testing."""
    created = sync_gmail(db)
    return {"new_invoices_queued": created}
