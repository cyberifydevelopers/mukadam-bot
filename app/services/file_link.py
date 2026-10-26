"""The "View file" link at the end of a WhatsApp prompt: opens the document the
data was read from (its PDF, or the email text), so the approver can check it.

The dashboard's own PDF route has no login and counts 1, 2, 3…, so the link
carries a signed token instead: only the invoice it was made for opens with it.
"""

import hashlib
import hmac
import os

from app.config import settings


def _secret() -> bytes:
    return (settings.file_link_secret or settings.openwa_webhook_secret).encode()


def _base_url() -> str:
    if settings.public_base_url:
        return settings.public_base_url.rstrip("/")
    # Railway sets this to the service's public domain.
    if settings.railway_public_domain:
        return f"https://{settings.railway_public_domain}"
    # Running on your own computer: the dashboard is at localhost. On Railway without a domain there is
    # no address worth sending, so no link.
    return "" if os.environ.get("RAILWAY_ENVIRONMENT") else "http://localhost:8000"


def _token(invoice_id: int) -> str:
    return hmac.new(_secret(), f"view-file:{invoice_id}".encode(), hashlib.sha256).hexdigest()[:32]


def is_valid(invoice_id: int, token: str) -> bool:
    return bool(_secret()) and hmac.compare_digest(_token(invoice_id), token or "")


def url_for(invoice_id: int) -> str | None:
    """None when the app has no address to give (on Railway without a domain) or no secret to sign with."""
    base = _base_url()
    if not base or not _secret():
        return None
    return f"{base}/view/{invoice_id}?t={_token(invoice_id)}"
