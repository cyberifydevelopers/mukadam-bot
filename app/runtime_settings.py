"""Settings that can be changed from the dashboard without a restart.

The value is saved in the signed-in user's own `app_settings` table, so each person
has their own. With nothing saved, the first user (whose data predates sign-in)
uses .env as before.
"""

import logging
import re

from sqlalchemy.orm import Session

from app import tenancy
from app.config import settings
from app.database import SessionLocal
from app.models import AppSetting

logger = logging.getLogger(__name__)

NOTIFY_NUMBERS_KEY = "notify_whatsapp_numbers"


class InvalidSetting(ValueError):
    pass


def notify_numbers() -> list[str]:
    """The WhatsApp number(s) that approve this user's documents."""
    with SessionLocal() as db:
        row = db.get(AppSetting, NOTIFY_NUMBERS_KEY)
    if row is not None:
        return [n for n in row.value.split(",") if n]
    return settings.notify_numbers_list if tenancy.is_first_user() else []


def all_notify_numbers() -> dict[str, int | None]:
    """Every approver number of every user -> the user it belongs to (to tell whose message a WhatsApp reply is)."""
    owners = {}
    for user_id in tenancy.user_ids():
        with tenancy.use(user_id):
            for number in notify_numbers():
                owners.setdefault(re.sub(r"\D", "", number), user_id)
    return owners


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
    logger.info("WhatsApp prompt number(s) changed from the dashboard to %s", value)
    return numbers
