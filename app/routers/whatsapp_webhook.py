import logging

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from app.config import settings
from app.confirmation import apply_answer, find_by_message_id, label_of
from app.database import get_db
from app.services import whatsapp_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook/whatsapp", tags=["whatsapp"])


@router.get("")
def verify_webhook(
    hub_mode: str = Query(alias="hub.mode", default=""),
    hub_verify_token: str = Query(alias="hub.verify_token", default=""),
    hub_challenge: str = Query(alias="hub.challenge", default=""),
):
    """Meta calls this once, at the time you register the webhook URL in the
    App Dashboard, to prove you control the endpoint."""
    if hub_mode == "subscribe" and hub_verify_token == settings.whatsapp_webhook_verify_token:
        return Response(content=hub_challenge, media_type="text/plain")
    return Response(status_code=403)


@router.post("")
async def receive_webhook(request: Request, db: Session = Depends(get_db)):
    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")

    if not whatsapp_service.verify_webhook_signature(raw_body, signature):
        logger.warning("Rejected WhatsApp webhook call with invalid/missing signature")
        return Response(status_code=403)

    payload = await request.json()
    replies = whatsapp_service.parse_button_replies(payload)

    for reply in replies:
        record = find_by_message_id(db, reply.context_wamid)
        if record is None:
            logger.warning("Button reply referenced unknown context id %s", reply.context_wamid)
            continue

        # None = already handled (e.g. Meta retried the webhook delivery) or
        # an unrecognized button label.
        ack = apply_answer(db, record, reply.button_text)
        if ack is None:
            continue

        try:
            whatsapp_service.send_text_message(reply.from_number, ack)
        except whatsapp_service.WhatsAppSendError as exc:
            logger.error("Failed to send acknowledgement for %s: %s", label_of(record), exc)

    db.commit()
    return {"status": "ok"}
