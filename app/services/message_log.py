"""Records WhatsApp traffic in the whatsapp_messages table for the dashboard."""

from sqlalchemy.orm import Session

from app.models import WhatsAppMessage

# Delivery only moves forward; a late "delivered" must not overwrite "read".
_STATUS_RANK = {"sent": 1, "delivered": 2, "read": 3}


def log_message(
    db: Session,
    *,
    direction: str,
    provider: str,
    peer_number: str | None,
    text: str,
    status: str,
    message_id: str | None = None,
    invoice_id: int | None = None,
    transaction_id: int | None = None,
    error: str | None = None,
) -> WhatsAppMessage:
    msg = WhatsAppMessage(
        direction=direction,
        provider=provider,
        peer_number=peer_number,
        text=text,
        status=status,
        message_id=message_id,
        invoice_id=invoice_id,
        transaction_id=transaction_id,
        error=error,
    )
    db.add(msg)
    return msg


def apply_delivery_status(db: Session, message_id: str, status: str) -> bool:
    """Updates an outgoing message's delivery status from a receipt. Returns
    False if the message isn't one we logged."""
    msg = db.query(WhatsAppMessage).filter(WhatsAppMessage.message_id == message_id).first()
    if msg is None:
        return False
    if status == "failed" or _STATUS_RANK.get(status, 0) > _STATUS_RANK.get(msg.status, 0):
        msg.status = status
    return True
