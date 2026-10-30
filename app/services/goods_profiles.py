"""What the bot remembers about a customer's goods, so the approver is asked only once.

The first time a firm's goods come in (say wheat for ABC Factory) the stock report lacks margin, weight and the
like, and the approver is reminded to send them. Once given — by a reply on WhatsApp, or by the bank's follow-up —
they are kept per customer and goods, and every later invoice or delivery order of that customer's wheat gets them
filled in without asking again. A figure typed in later replaces the saved one.

Goods are told apart by their first word ("Wheat Bags 50Kgs", "Wheat Hgr 20Kgs" → wheat) spelled loosely, so that
"Putti", "Putty" and "Phutty" from different invoices are one goods; the customer by its name without "M/s" or
"(Pvt) Ltd". A rate in a foreign currency is never kept: it would not fit a rupee invoice.
"""

import logging
import re
from datetime import datetime

from app.database import SessionLocal
from app.models import GoodsProfile

logger = logging.getLogger(__name__)

# The figures kept, as keys of a stock line, with how they are named to the approver.
FIELDS = {
    "margin_pct": "Margin %",
    "weight_per_unit": "Weight per unit",
    "weight_measurement": "Weight measurement per unit",
    "rate_per_unit": "Rate per unit",
}

_COMPANY_WORDS = re.compile(r"\b(m/?s|pvt|private|ltd|limited|co|company)\b\.?", re.IGNORECASE)


def customer_key(order: dict) -> str:
    return re.sub(r"[^a-z0-9]", "", _COMPANY_WORDS.sub(" ", (order.get("customer_name") or "").lower()))


def loose(word: str) -> str:
    """A word spelled loosely, so spellings of one word are equal: 'putti' = 'putty' = 'phutty' = 'puti'."""
    return re.sub(r"(.)\1+", r"\1", word.lower().replace("ph", "p").replace("y", "i"))


def goods_key(description: str | None) -> str:
    """The commodity: the first word of the description that is a word ("Wheat Bags 50Kgs" → "wheat"), spelled
    loosely ("Putty" and "Putti" are the same key)."""
    match = re.search(r"[a-z]{3,}", (description or "").lower())
    return loose(match.group(0)) if match else ""


def goods_label(description: str | None) -> str:
    """The goods as the document spells them ("Putty (munds)" → "Putty"), for showing to people."""
    match = re.search(r"[A-Za-z]{3,}", description or "")
    return match.group(0).title() if match else ""


def _rupees(order: dict) -> bool:
    return (order.get("currency") or "").strip().upper() in ("", "PKR", "RS", "RS.")


def _has(value) -> bool:
    return value not in (None, "") and str(value).strip() != ""


def apply(order: dict) -> list[str]:
    """Fills the figures a stock line lacks from what is saved for its customer and goods. Returns what was used,
    one line per goods, for the approver ("Wheat: Margin %: 5, Weight per unit: 50")."""
    customer = customer_key(order)
    if not customer:
        return []
    notes = []
    try:
        with SessionLocal() as db:
            for row in order.get("rows") or []:
                goods = goods_key(row.get("description"))
                profile = db.query(GoodsProfile).filter_by(customer_key=customer, goods_key=goods).first() if goods else None
                if profile is None:
                    continue
                used = []
                for field, label in FIELDS.items():
                    saved = getattr(profile, field)
                    if _has(saved) and not _has(row.get(field)) and (field != "rate_per_unit" or _rupees(order)):
                        row[field] = saved
                        used.append(f"{label}: {saved}")
                if used:
                    notes.append(f"{goods.title()} — {', '.join(used)}")
    except Exception:  # noqa: BLE001 — remembering is a convenience; the report must still be made without it
        logger.exception("Couldn't read the saved goods figures")
    return notes


def remember(order: dict) -> None:
    """Saves the figures the stock lines now carry, for the next invoice of the same customer and goods."""
    customer = customer_key(order)
    if not customer:
        return
    try:
        with SessionLocal() as db:
            for row in order.get("rows") or []:
                goods = goods_key(row.get("description"))
                if not goods:
                    continue
                values = {f: str(row[f]).strip() for f in FIELDS if _has(row.get(f)) and (f != "rate_per_unit" or _rupees(order))}
                if not values:
                    continue
                profile = db.query(GoodsProfile).filter_by(customer_key=customer, goods_key=goods).first()
                if profile is None:
                    profile = GoodsProfile(customer_key=customer, goods_key=goods)
                    db.add(profile)
                for field, value in values.items():
                    setattr(profile, field, value)
                profile.customer_name = (order.get("customer_name") or "").strip() or profile.customer_name
                profile.goods_label = profile.goods_label or goods_label(row.get("description"))
                profile.updated_at = datetime.utcnow()
            db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("Couldn't save the goods figures")


def migrate_keys() -> int:
    """Run once at startup: profiles saved before keys were spelled loosely ('putti' and 'putty' as two goods) are merged
    into one per customer — the most recently saved figures win — and keep the older spelling as their display name.
    Returns how many rows were changed or removed."""
    changed = 0
    try:
        with SessionLocal() as db:
            groups: dict[tuple[str, str], list[GoodsProfile]] = {}
            for profile in db.query(GoodsProfile).order_by(GoodsProfile.updated_at, GoodsProfile.id):
                groups.setdefault((profile.customer_key, loose(profile.goods_key)), []).append(profile)
            for (_, key), rows in groups.items():
                if len(rows) == 1 and rows[0].goods_key == key:
                    continue
                keeper = rows[-1]  # the newest
                for older in rows[:-1]:  # oldest first, so a newer figure overrides an older one
                    for field in FIELDS:
                        if not _has(getattr(keeper, field)) and _has(getattr(older, field)):
                            setattr(keeper, field, getattr(older, field))
                    keeper.goods_label = keeper.goods_label or older.goods_label or older.goods_key.title()
                    keeper.customer_name = keeper.customer_name or older.customer_name
                    db.delete(older)
                    changed += 1
                db.flush()  # the unique (customer, goods) key must be free before the keeper takes it
                keeper.goods_label = keeper.goods_label or keeper.goods_key.title()
                if keeper.goods_key != key:
                    keeper.goods_key = key
                    changed += 1
            db.commit()
    except Exception:  # noqa: BLE001 — merging is housekeeping; the app must start without it
        logger.exception("Couldn't merge the saved goods figures")
    if changed:
        logger.info("Merged saved goods figures: %d row(s) changed", changed)
    return changed
