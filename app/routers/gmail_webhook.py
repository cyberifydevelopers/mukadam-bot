import base64
import json
import logging
import threading

from fastapi import APIRouter, Query, Request, Response

from app import tenancy
from app.config import settings
from app.database import SessionLocal
from app.pipeline import sync_gmail

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook/gmail", tags=["gmail"])


@router.post("")
async def receive_gmail_push(
    request: Request,
    token: str = Query(default=""),
):
    """Google Cloud Pub/Sub push endpoint. Gmail's `users.watch()` makes
    Gmail publish to a Pub/Sub topic on every mailbox change; the push
    subscription on that topic (created via gcloud/Console — see
    docs/WORKFLOW.md) delivers each notification here as an HTTP POST.

    This URL is otherwise unauthenticated, so a shared `token` query
    parameter (set when the push subscription's endpoint URL is created) is
    checked before doing anything — Pub/Sub supports exactly this pattern
    for push endpoint verification.
    """
    if not settings.pubsub_verification_token or token != settings.pubsub_verification_token:
        logger.warning("Rejected Gmail push call with invalid/missing verification token")
        return Response(status_code=403)

    envelope = await request.json()
    message = envelope.get("message", {})
    data_b64 = message.get("data", "")

    if data_b64:
        # Payload is {"emailAddress": "...", "historyId": "..."} — informational
        # only. We deliberately do NOT use this historyId as our sync cursor
        # (see sync_gmail): Pub/Sub delivery can be out of order or
        # duplicated, so we always resync from our own stored cursor.
        payload = json.loads(base64.b64decode(data_b64).decode("utf-8"))
        logger.info(
            "Gmail push received: mailbox=%s historyId=%s",
            payload.get("emailAddress"),
            payload.get("historyId"),
        )

    # Reading a document takes a minute, far past Pub/Sub's ack deadline (and would freeze the server meanwhile):
    # answer at once and sync in the background. A sync that fails is picked up by the periodic check.
    threading.Thread(target=_sync_everyone, name="gmail-push-sync", daemon=True).start()

    # Pub/Sub retries on anything but a 2xx, so always ack.
    return {"status": "ok"}


def _sync_everyone() -> None:
    for user_id in tenancy.user_ids():
        with tenancy.use(user_id), SessionLocal() as db:
            try:
                created = sync_gmail(db)
                if created:
                    logger.info("Gmail push triggered sync, queued %d new invoice(s) (user %s)", created, user_id)
            except Exception:
                logger.exception("Sync after Gmail push failed (user %s)", user_id)
                db.rollback()
