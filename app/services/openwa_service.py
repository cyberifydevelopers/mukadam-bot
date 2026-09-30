"""OpenWA integration — an alternative to the Meta Cloud API
(app/services/whatsapp_service.py) that drives a regular WhatsApp account
through a self-hosted OpenWA gateway (WhatsApp Web, linked by QR scan).

Differences from the Meta path that shape this module:

1. No templates and no buttons: the prompt is plain text and the approver
   answers by typing YES or NO. Free-form text is allowed at any time, so
   there is no 24h-session constraint either.

2. Correlation: the approver reacts 👍/👎 to the prompt (OpenWA reports
   the reacted-to message id), or replies (swipe → Reply) to it with
   YES/NO (OpenWA reports the quoted message's id) — the same role Meta's
   `context.id` plays. As a fallback for a plain un-quoted message, the
   prompt also carries its id ("YES 7" / "YES T12"). WhatsApp Web accounts
   cannot SEND buttons (only the official Cloud API can), and OpenWA does
   not report poll votes, so reactions are the one-tap answer here.

3. Unofficial: OpenWA automates WhatsApp Web, which WhatsApp does not
   sanction. Use a dedicated number for the bot.
"""

import hashlib
import hmac
import logging
import re
from dataclasses import dataclass

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class OpenWaSendError(Exception):
    pass


def _chat_id(number: str) -> str:
    """E.164 (+923001234567) or bare digits → OpenWA chat id (923001234567@c.us)."""
    return re.sub(r"\D", "", number) + "@c.us"


def send_text(to_number: str, text: str) -> str:
    """Sends a plain text message and returns its message id (used to
    correlate a quoted reply back to the invoice)."""
    if not settings.openwa_api_key or not settings.openwa_session_id:
        raise OpenWaSendError("OPENWA_API_KEY / OPENWA_SESSION_ID not configured")

    url = f"{settings.openwa_url.rstrip('/')}/api/sessions/{settings.openwa_session_id}/messages/send-text"
    try:
        resp = httpx.post(
            url,
            json={"chatId": _chat_id(to_number), "text": text},
            headers={"X-API-Key": settings.openwa_api_key},
            timeout=30,
        )
    except httpx.HTTPError as exc:
        raise OpenWaSendError(f"OpenWA unreachable at {settings.openwa_url}: {exc}") from exc

    if resp.status_code >= 400:
        raise OpenWaSendError(f"OpenWA error {resp.status_code}: {resp.text}")

    data = resp.json()
    try:
        return data["messageId"]
    except KeyError as exc:
        raise OpenWaSendError(f"Unexpected OpenWA response: {data}") from exc


def session_status() -> dict:
    """The linked session as OpenWA reports it (status, phone, pushName…),
    or {"status": "unreachable", "error": ...} — for the dashboard."""
    if not settings.openwa_api_key or not settings.openwa_session_id:
        return {"status": "not_configured"}
    try:
        resp = httpx.get(
            f"{settings.openwa_url.rstrip('/')}/api/sessions/{settings.openwa_session_id}",
            headers={"X-API-Key": settings.openwa_api_key},
            timeout=5,
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:
        return {"status": "unreachable", "error": str(exc)}


def verify_webhook_signature(raw_body: bytes, signature_header: str | None, url_token: str = "") -> bool:
    """Validates OpenWA's X-OpenWA-Signature header (`sha256=` + HMAC-SHA256
    of the raw body, keyed with the secret set when the webhook was
    registered). Without it, anyone who can reach the endpoint could forge a
    YES and write fabricated rows into the sheet.

    A webhook created in OpenWA's dashboard has no secret (the form has no
    field for one), so it sends no signature; for those, the secret can ride
    in the webhook URL instead (`?token=<OPENWA_WEBHOOK_SECRET>`), the same
    scheme the Gmail push endpoint uses.
    """
    if not settings.openwa_webhook_secret:
        logger.warning("OPENWA_WEBHOOK_SECRET not set — refusing to accept unverifiable webhook calls")
        return False
    if not signature_header or not signature_header.startswith("sha256="):
        if url_token:
            if hmac.compare_digest(url_token.encode(), settings.openwa_webhook_secret.encode()):
                return True
            logger.warning("OpenWA webhook ?token= doesn't match OPENWA_WEBHOOK_SECRET")
            return False
        logger.warning(
            "OpenWA webhook call has no X-OpenWA-Signature header and no ?token= — add "
            "?token=<OPENWA_WEBHOOK_SECRET> to the webhook URL in OpenWA"
        )
        return False

    expected = hmac.new(settings.openwa_webhook_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature_header.split("=", 1)[1]):
        logger.warning(
            "OpenWA webhook signature mismatch — the secret set on the webhook in OpenWA "
            "differs from OPENWA_WEBHOOK_SECRET"
        )
        return False
    return True


@dataclass
class TextReply:
    sender_phone: str  # digits only; "" when OpenWA couldn't resolve it
    answer: str  # "yes" / "no"
    # From "YES 7" (invoice #7) or "YES T12" (statement transaction T12);
    # None when only the word was sent.
    target_label: str | None
    quoted_message_id: str | None  # id of the prompt this replies to, if quoted
    message_id: str
    text: str  # the message as typed


@dataclass
class Reaction:
    message_id: str  # the message that was reacted to (our prompt)
    answer: str  # "yes" / "no"
    emoji: str


_REPLY_RE = re.compile(r"^\s*(yes|y|no|n)\b\W*(t?\d+)?\s*$", re.IGNORECASE)

# Skin-tone variants of 👍/👎 arrive with a modifier appended, so compare the
# base character only.
_YES_EMOJI = {"👍", "✅", "✔", "☑", "👌", "💯"}
_NO_EMOJI = {"👎", "❌", "✖", "🚫"}


# "…@lid" → approver phone digits, learned from OpenWA's contacts/check.
_LID_TO_PHONE: dict[str, str] = {}


def _phone_for_lid(lid: str) -> str:
    """Maps a sender's @lid privacy id back to a phone number by asking
    OpenWA which @lid each approver number (NOTIFY_WHATSAPP_NUMBERS) has.
    Only approvers need resolving — nobody else's YES is ever acted on."""
    if lid not in _LID_TO_PHONE:
        for number in settings.notify_numbers_list:
            digits = re.sub(r"\D", "", number)
            if digits in _LID_TO_PHONE.values():
                continue
            try:
                resp = httpx.get(
                    f"{settings.openwa_url.rstrip('/')}/api/sessions/{settings.openwa_session_id}"
                    f"/contacts/check/{digits}",
                    headers={"X-API-Key": settings.openwa_api_key},
                    timeout=15,
                )
                resp.raise_for_status()
                whatsapp_id = resp.json().get("whatsappId") or ""
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("OpenWA contacts/check for %s failed: %s", digits, exc)
                continue
            if whatsapp_id.endswith("@lid"):
                _LID_TO_PHONE[whatsapp_id] = digits
    return _LID_TO_PHONE.get(lid, "")


def _sender_phone(data: dict) -> str:
    sender = data.get("from", "")
    if sender.endswith("@c.us"):
        return sender.split("@", 1)[0]
    # @lid privacy id — OpenWA resolves the real number when
    # RESOLVE_LID_TO_PHONE=true is set on the gateway. Without that, a 1:1
    # chat's chatId may still be the other party's @c.us id (groups are
    # filtered out before this is called); failing both, ask OpenWA which
    # approver number that @lid belongs to.
    chat_id = data.get("chatId") or ""
    phone = (
        data.get("senderPhone")
        or (data.get("contact") or {}).get("number")
        or (chat_id.split("@", 1)[0] if chat_id.endswith("@c.us") else "")
    )
    if not phone:
        lid = sender if sender.endswith("@lid") else chat_id if chat_id.endswith("@lid") else ""
        if lid:
            phone = _phone_for_lid(lid)
    if not phone:
        logger.warning(
            "OpenWA message from %s (chatId %s) matches no approver number; payload fields: %s",
            sender,
            chat_id,
            sorted(data.keys()),
        )
    return re.sub(r"\D", "", phone)


def parse_reply(webhook_payload: dict) -> TextReply | None:
    """Extracts a YES/NO answer from a `message.received` delivery. Returns
    None for anything else (other events, groups, our own messages, text
    that isn't a yes/no) — those are ignored by design.
    """
    if webhook_payload.get("event") != "message.received":
        return None
    data = webhook_payload.get("data") or {}
    if data.get("fromMe") or data.get("isGroup") or data.get("isStatusBroadcast"):
        return None

    body = data.get("body") or ""
    match = _REPLY_RE.match(body)
    if not match:
        return None

    word, label = match.groups()
    return TextReply(
        sender_phone=_sender_phone(data),
        answer="yes" if word.lower().startswith("y") else "no",
        target_label=label.upper() if label else None,
        quoted_message_id=(data.get("quotedMessage") or {}).get("id"),
        message_id=data.get("id", ""),
        text=body,
    )


def parse_reaction(webhook_payload: dict) -> Reaction | None:
    """A 👍/👎 (or ✅/❌) reaction on one of our prompts, from a
    `message.reaction` delivery. Removing a reaction, or any other emoji,
    returns None."""
    if webhook_payload.get("event") != "message.reaction":
        return None
    data = webhook_payload.get("data") or {}
    emoji = (data.get("reaction") or "").strip()
    if not emoji or not data.get("messageId"):
        return None
    base = emoji[0]
    if base in _YES_EMOJI:
        answer = "yes"
    elif base in _NO_EMOJI:
        answer = "no"
    else:
        return None
    return Reaction(message_id=data["messageId"], answer=answer, emoji=emoji)


def react(to_number: str, message_id: str, emoji: str) -> None:
    """Puts the bot's own reaction on a message (✅ saved / ❌ skipped), so the
    approver sees the outcome on the prompt itself without an extra message
    per transaction."""
    url = f"{settings.openwa_url.rstrip('/')}/api/sessions/{settings.openwa_session_id}/messages/react"
    try:
        resp = httpx.post(
            url,
            json={"chatId": _chat_id(to_number), "messageId": message_id, "emoji": emoji},
            headers={"X-API-Key": settings.openwa_api_key},
            timeout=30,
        )
    except httpx.HTTPError as exc:
        raise OpenWaSendError(f"OpenWA unreachable at {settings.openwa_url}: {exc}") from exc
    if resp.status_code >= 400:
        raise OpenWaSendError(f"OpenWA react error {resp.status_code}: {resp.text}")
