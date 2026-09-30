"""Settings that can be changed from the dashboard without a restart.

The value is saved in the `app_settings` table and written onto the shared
`settings` object, so every reader (pipeline, webhooks, dashboard) sees it
at once. On startup, saved values override .env; with nothing saved, .env
is used as before.
"""

import logging
import re

from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models import AppSetting

logger = logging.getLogger(__name__)

NOTIFY_NUMBERS_KEY = "notify_whatsapp_numbers"


class InvalidSetting(ValueError):
    pass


def load_overrides() -> None:
    with SessionLocal() as db:
        row = db.get(AppSetting, NOTIFY_NUMBERS_KEY)
        if row is not None:
            settings.notify_whatsapp_numbers = row.value
            logger.info("WhatsApp prompt number(s) from dashboard setting: %s", row.value)


def notify_numbers_source(db: Session) -> str:
    return "dashboard" if db.get(AppSetting, NOTIFY_NUMBERS_KEY) is not None else ".env"


def set_notify_numbers(db: Session, raw: str) -> list[str]:
    """Accepts one or more numbers (comma-separated), with or without "+",
    spaces or dashes, and stores them as bare digits with country code."""
    numbers = []
    for part in raw.split(","):
        if not part.strip():
            continue
        digits = re.sub(r"[\s\-()+]", "", part)
        if not digits.isdigit() or not 10 <= len(digits) <= 15:
            raise InvalidSetting(
                f"'{part.strip()}' isn't a valid number. Use the full number with country code, e.g. 923001234567"
            )
        numbers.append(digits)
    if not numbers:
        raise InvalidSetting("Enter at least one number")

    value = ",".join(numbers)
    row = db.get(AppSetting, NOTIFY_NUMBERS_KEY)
    if row is None:
        db.add(AppSetting(key=NOTIFY_NUMBERS_KEY, value=value))
    else:
        row.value = value
    db.commit()
    settings.notify_whatsapp_numbers = value
    logger.info("WhatsApp prompt number(s) changed from the dashboard to %s", value)
    return numbers
