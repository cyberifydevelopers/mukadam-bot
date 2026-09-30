"""WhatsApp Cloud API (Meta) integration.

Two hard constraints from the real WhatsApp Business Platform, not
assumptions:

1. This bot is the one starting the conversation (an email arrived, nobody
   messaged the bot first). Meta only allows a business to start a
   conversation with a pre-approved MESSAGE TEMPLATE — a free-form
   "interactive buttons" message can only be sent inside a 24h session that
   the *user* opened. So the Yes/No prompt must go out as a template
   (`WHATSAPP_TEMPLATE_NAME` in .env), with two static Quick-Reply buttons
   (e.g. labelled "Yes" / "No") configured when the template was created and
   approved in Meta Business Manager. See docs/WORKFLOW.md for the exact
   template body used here.

2. A template's quick-reply button payload is STATIC (fixed at template
   creation, identical on every send) — it cannot be parameterised per
   invoice. So we cannot encode the invoice id in the button payload. The
   only way to know *which* invoice a "Yes"/"No" reply is answering is the
   `context.id` field WhatsApp attaches to the reply, which equals the WAMID
   (WhatsApp message id) of the template message we sent. That WAMID is
   stored on InvoiceRecord.whatsapp_message_id at send time and looked back
   up when the webhook fires.
"""

import hashlib
import hmac
import logging
from dataclasses import dataclass

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

GRAPH_API_VERSION = "v20.0"


class WhatsAppSendError(Exception):
    pass


def _messages_url() -> str:
    return f"https://graph.facebook.com/{GRAPH_API_VERSION}/{settings.whatsapp_phone_number_id}/messages"


def send_confirmation_template(to_number: str, body_params: list[str]) -> str:
    """Sends the approved confirmation template and returns the WAMID of the
    sent message (used later to correlate the button reply).

    `body_params` fill the template's `{{1}}`, `{{2}}`, ... placeholders in
    order — see docs/WORKFLOW.md for the template body used by this project.
    """
    if not settings.whatsapp_access_token or not settings.whatsapp_phone_number_id:
        raise WhatsAppSendError("WHATSAPP_ACCESS_TOKEN / WHATSAPP_PHONE_NUMBER_ID not configured")

    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "template",
        "template": {
            "name": settings.whatsapp_template_name,
            "language": {"code": settings.whatsapp_template_lang},
            "components": [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": p} for p in body_params],
                }
            ],
        },
    }
    headers = {"Authorization": f"Bearer {settings.whatsapp_access_token}"}

    resp = httpx.post(_messages_url(), json=payload, headers=headers, timeout=30)
    if resp.status_code >= 400:
        raise WhatsAppSendError(f"WhatsApp API error {resp.status_code}: {resp.text}")

    data = resp.json()
    try:
        return data["messages"][0]["id"]
    except (KeyError, IndexError) as exc:
        raise WhatsAppSendError(f"Unexpected WhatsApp API response: {data}") from exc


def send_text_message(to_number: str, text: str) -> str:
    """Free-form text — only valid within the 24h session window the user
    opened by tapping Yes/No. Used for the "saved to excel" acknowledgement.
    """
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": text},
    }
    headers = {"Authorization": f"Bearer {settings.whatsapp_access_token}"}

    resp = httpx.post(_messages_url(), json=payload, headers=headers, timeout=30)
    if resp.status_code >= 400:
        raise WhatsAppSendError(f"WhatsApp API error {resp.status_code}: {resp.text}")

    data = resp.json()
    return data["messages"][0]["id"]


def verify_webhook_signature(raw_body: bytes, signature_header: str | None) -> bool:
    """Validates Meta's X-Hub-Signature-256 header (HMAC-SHA256 of the raw
    request body, keyed with the app secret). Without this check, anyone who
    discovers the webhook URL could forge "Yes" confirmations that write
    fabricated rows into the bank Excel sheets.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    if not settings.whatsapp_app_secret:
        logger.warning("WHATSAPP_APP_SECRET not set — refusing to accept unverifiable webhook calls")
        return False

    expected = hmac.new(
        settings.whatsapp_app_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    provided = signature_header.split("=", 1)[1]
    return hmac.compare_digest(expected, provided)


@dataclass
class ButtonReply:
    from_number: str
    button_text: str  # e.g. "Yes" / "No" — the static label configured on the template
    context_wamid: str  # WAMID of the template message this is a reply to
    wamid: str  # WAMID of this reply itself (for dedup)


def parse_button_replies(webhook_payload: dict) -> list[ButtonReply]:
    """Extracts quick-reply button taps from a WhatsApp webhook POST body.
    Returns [] for payloads that aren't a button reply (status callbacks,
    plain text messages, etc.) — those are ignored by design.
    """
    replies: list[ButtonReply] = []
    for entry in webhook_payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for message in value.get("messages", []):
                if message.get("type") != "button":
                    continue
                button = message.get("button", {})
                context = message.get("context", {})
                if not context.get("id"):
                    continue
                replies.append(
                    ButtonReply(
                        from_number=message.get("from", ""),
                        button_text=button.get("text", ""),
                        context_wamid=context["id"],
                        wamid=message.get("id", ""),
                    )
                )
    return replies
