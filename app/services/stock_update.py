"""A bank's follow-up to a delivery order already confirmed: the same reference, now with the figures the first
letter lacked (rate, margin %, drums, weight). Instead of a second record asking for a second YES, its figures are
added to the first record's stock lines and that record's stock report is made again and sent.

The bank may answer in any form, so the follow-up is matched to the earlier order by its reference number (a number
the first reading got one character wrong still matches when the customer is the same) and its lines are paired with
the earlier lines by their quantities, else by their goods names, else by position.
"""

import json
import logging
import re
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models import InvoiceRecord, InvoiceStatus
from app.services import goods_profiles, stock_report

logger = logging.getLogger(__name__)

# The row figures a follow-up can add (the keys of stock_report.FIGURE_KEYS' values).
ROW_FIGURES = {
    "rate_per_unit": "Rate per unit",
    "margin_pct": "Margin %",
    "drums": "No of drums",
    "weight_per_unit": "Weight per unit",
    "weight_measurement": "Weight measurement per unit",
}


_QUANTITY_KEYS = ("opening_balance", "quantity_received", "quantity_released", "balance_after")
_SKIP_WORDS = {"for", "the", "and", "of", "factory", "mill", "mills", "industries", "industry", "pvt", "ltd", "limited", "company",
               "rate", "margin", "weight", "drums", "price", "per", "unit", "report", "please", "add", "set", "save"}
# Figure names (as the WhatsApp parser calls them) → the columns of a goods profile; drums are not kept.
_PROFILE_FIELD = {"rate": "rate_per_unit", "margin": "margin_pct", "weight_per_unit": "weight_per_unit", "weight_measurement": "weight_measurement"}


def _alnum(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _within_one(a: str, b: str) -> bool:
    """Whether two references differ by at most one character (a misread digit, a dropped slash)."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) <= 1
    short, long_ = sorted((a, b), key=len)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


def _has_figures(order: dict) -> bool:
    return any(row.get(key) not in (None, "") for row in order.get("rows") or [] for key in ROW_FIGURES)


def _same_customer(a: str, b: str) -> bool:
    return len(a) >= 4 and len(b) >= 4 and (a == b or a in b or b in a)


def find_existing(db: Session, record: InvoiceRecord, order: dict) -> InvoiceRecord | None:
    """The confirmed delivery order this record continues, or None: the same reference, or one character off
    when it is the same customer. A follow-up that names no reference at all (a short email, a photo) goes to the
    customer's latest confirmed order of the last 30 days."""
    ref = _alnum(record.reference_number)
    if not _has_figures(order):
        return None
    customer = _alnum(stock_report._customer(order)) if (order.get("customer_name") or "").strip() else ""
    if not ref and not customer:
        return None
    recent = datetime.utcnow() - timedelta(days=30)
    earlier = [
        (old, old_order) for old in db.query(InvoiceRecord)
        .filter(InvoiceRecord.id != record.id, InvoiceRecord.status == InvoiceStatus.CONFIRMED).order_by(InvoiceRecord.id.desc())
        if (old_order := stock_report.delivery_order(old))
    ]

    def same_customer(old_order: dict) -> bool:
        return bool(customer) and _same_customer(customer, _alnum(stock_report._customer(old_order)))

    if ref:
        for old, old_order in earlier:
            old_ref = _alnum(old.reference_number)
            if old_ref and (old_ref == ref or (same_customer(old_order) and _within_one(old_ref, ref))):
                return old
        # A message that only gives figures may carry a number that is not a delivery order's reference (a CP /
        # account number in a margin table): then it is matched by customer and goods like one with no reference.
        if not figures_only(order):
            return None
    new_goods = {g for row in order.get("rows") or [] if (g := goods_profiles.goods_key(row.get("description")))}
    for old, old_order in earlier:
        if not same_customer(old_order) or (old.created_at or recent) < recent:
            continue
        old_goods = {g for row in old_order.get("rows") or [] if (g := goods_profiles.goods_key(row.get("description")))}
        if new_goods and not new_goods & old_goods:
            continue  # rice figures don't go to a wheat order
        return old
    return None


def _by_goods(text: str, commodities: list[str]) -> dict[str, tuple[str, str]]:
    """"PUTTI 3,800 COTTON BALES 85,000" or "PUTTI 25% COTTON BALES 20%" → {"putti": ("PUTTI", "3800"), …}. A single
    bare figure ("20%", "3,800") is for every commodity the table approves."""
    bare = re.fullmatch(r"\s*(?:rs\.?|pkr)?\s*(\d[\d,]*(?:\.\d+)?)\s*(?:%|kgs?)?\s*", text, re.IGNORECASE)
    if bare:
        return {g.lower(): (g, bare.group(1).replace(",", "")) for g in commodities}
    found = re.findall(r"([A-Za-z][A-Za-z &/.\-]*?)\s*(?:rs\.?|pkr)?\s*(\d[\d,]*(?:\.\d+)?)\s*(?:%|kgs?\b)?", text, re.IGNORECASE)
    if len(found) == 1 and commodities:  # one figure, named or not: the table's single figure for all of them
        return {g.lower(): (g, found[0][1].replace(",", "")) for g in commodities}
    return {g.strip(" -&/.").lower(): (g.strip(" -&/."), n.replace(",", "")) for g, n in found if g.strip(" -&/.")}


def orders_from_table(items_table: dict | None) -> list[dict]:
    """A bank's pledge-site / muccadam appointment table (Name of Borrower, Margin, Rate Per Unit, Weight Per Unit,
    Approved Commodities…), read straight from the table the model returned: one figures-only order per borrower, with
    each commodity's margin, rate and weight. [] when the table isn't one. Not left to the model, so a table that
    was not recognised as a delivery order is still used."""
    columns = [str(c).lower() for c in (items_table or {}).get("columns") or []]

    def column(*words: str, but: tuple = ()) -> int | None:
        return next((i for i, c in enumerate(columns) if any(w in c for w in words) and not any(x in c for x in but)), None)

    borrower, commodities, location = column("borrower"), column("commodit"), column("location")
    figures = {"margin_pct": column("margin"), "rate_per_unit": column("rate", "price"), "weight_per_unit": column("weight", but=("measure",))}
    if borrower is None or not any(v is not None for v in figures.values()):
        return []
    orders = []
    for row in (items_table or {}).get("rows") or []:
        def cell(i: int | None) -> str:
            return str(row[i]).strip() if i is not None and i < len(row) and row[i] is not None else ""

        goods = [g.strip() for g in re.split(r"[,;/]", cell(commodities)) if g.strip()]
        rows: dict[str, dict] = {}
        for key, index in figures.items():
            for lower, (name, value) in _by_goods(cell(index), goods).items():
                rows.setdefault(lower, {"description": name})[key] = value
        if cell(borrower) and rows:
            orders.append({"customer_name": cell(borrower), "pledge_location": cell(location) or None, "rows": list(rows.values())})
    return orders


def follow_ups(db: Session, record: InvoiceRecord, extra: dict) -> dict | None:
    """What a message that only gives figures (rate, margin…) did: added to an earlier order, or kept for the
    factory's goods. {"merges": [{"into", "applied"}], "profiles": [{"factory", "notes"}]}, or None when it isn't one."""
    orders = [extra["delivery_order"]] if extra.get("delivery_order") else orders_from_table(extra.get("items_table"))
    merges, profiles = [], []
    for order in orders:
        earlier = find_existing(db, record, order)
        applied = merge(earlier, order) if earlier else []
        if applied:
            merges.append({"into": earlier.id, "applied": applied})
        elif figures_only(order) and (saved := save_from_order(db, order)):
            profiles.append({"factory": saved[0], "notes": saved[1]})
    return {"merges": merges, "profiles": profiles} if merges or profiles else None


def announce_all(db: Session, record: InvoiceRecord, extra: dict) -> None:
    """Sends the approver what follow_ups() did: each updated stock report, and what was kept for which factory."""
    for item in extra.get("merges") or []:
        old = db.get(InvoiceRecord, item["into"])
        try:
            announce(db, old, record, item["applied"])
        except Exception:  # noqa: BLE001 — the merge is saved; a failed send must not fail the email
            logger.exception("Couldn't send the updated report of #%s", item["into"])
    for item in extra.get("profiles") or []:
        try:
            announce_profile(db, record, item["factory"], item["notes"])
        except Exception:  # noqa: BLE001
            logger.exception("Couldn't tell the approver what was saved from #%s", record.id)


def _pair(old_rows: list[dict], new_rows: list[dict]) -> list[tuple[dict, dict]]:
    """Each new line with the earlier line it describes: same quantities first, then the closest goods name,
    then (when both have the same number of lines) position."""
    def quantities(row: dict) -> tuple:
        return tuple(stock_report._quantity(row.get(k)) for k in ("opening_balance", "quantity_released", "balance_after"))

    pairs: list[tuple[dict, dict]] = []
    used: set[int] = set()
    for j, new in enumerate(new_rows):
        best, best_score = None, 0.0
        for i, old in enumerate(old_rows):
            if i in used:
                continue
            q_new, q_old = quantities(new), quantities(old)
            same_qty = any(a is not None and a != 0 and a == b for a, b in zip(q_new, q_old))
            score = 2.0 if same_qty else stock_report._similarity(
                stock_report._words(new.get("description")), stock_report._words(old.get("description"))
            )
            if score > best_score:
                best, best_score = i, score
        if best is not None and best_score >= 0.5:
            used.add(best)
            pairs.append((old_rows[best], new))
        elif len(old_rows) == len(new_rows) and j not in used and goods_profiles.goods_key(new.get("description")) in ("", goods_profiles.goods_key(old_rows[j].get("description"))):
            used.add(j)  # same position, and not obviously other goods
            pairs.append((old_rows[j], new))
    # A line naming no goods and no quantities ("rate 3800" in a short message) is for every line not paired yet.
    for new in new_rows:
        if not (new.get("description") or "").strip() and not any(quantities(new)) and not any(n is new for _, n in pairs):
            pairs += [(old_rows[i], new) for i in range(len(old_rows)) if i not in used]
    return pairs


def merge(old: InvoiceRecord, new_order: dict) -> list[str]:
    """Adds the follow-up's figures to the earlier record's stock lines. Returns what changed, in words."""
    raw = json.loads(old.raw_extracted_json or "{}")
    order = raw.get("delivery_order") or {}
    applied = []
    for old_row, new_row in _pair(order.get("rows") or [], new_order.get("rows") or []):
        for key, label in ROW_FIGURES.items():
            value = new_row.get(key)
            if value in (None, "") or old_row.get(key) == value:
                continue
            old_row[key] = value
            applied.append(f"{label}: {value}")
    if new_order.get("currency") and not order.get("currency"):
        order["currency"] = new_order["currency"]
    raw["delivery_order"] = order
    old.raw_extracted_json = json.dumps(raw)
    return list(dict.fromkeys(applied))


def figures_only(order: dict) -> bool:
    """A message that only gives figures (rate, margin…) for goods: no quantities on any line, so it is not
    a delivery order of its own."""
    rows = order.get("rows") or []
    return bool(rows) and _has_figures(order) and not any(row.get(k) not in (None, "") for row in rows for k in _QUANTITY_KEYS)


def known_factories(db: Session) -> dict[str, str]:
    """Customer key → name, for every factory with a confirmed delivery order or saved figures."""
    from app.models import GoodsProfile
    from app.services import goods_profiles

    factories = {p.customer_key: p.customer_name or p.customer_key for p in db.query(GoodsProfile)}
    for record in db.query(InvoiceRecord).filter(InvoiceRecord.status == InvoiceStatus.CONFIRMED).order_by(InvoiceRecord.id):
        order = stock_report.delivery_order(record)
        key = goods_profiles.customer_key(order) if order else ""
        if key:
            factories[key] = stock_report._customer(order)
    return factories


def factory_in_text(db: Session, text: str) -> tuple[str, str] | None:
    """The one known factory a message names: its whole name, else its first distinctive word. None when it names
    none or several."""
    from app.services import goods_profiles

    flat = _alnum(text)
    words = set(re.findall(r"[a-z]{4,}", text.lower()))
    factories = known_factories(db)
    hits = [(k, n) for k, n in factories.items() if k and k in flat]
    if not hits:
        for key, name in factories.items():
            first = next(iter(re.findall(r"[a-z]{4,}", goods_profiles._COMPANY_WORDS.sub(" ", name.lower()))), None)
            if first and first in words and first not in _SKIP_WORDS:
                hits.append((key, name))
    return hits[0] if len(hits) == 1 else None


def save_profile_figures(db: Session, key: str, name: str, figures: list[tuple[str, str, str]]) -> list[str]:
    """Keeps figures typed for a factory's goods ("wheat margin 5") without any order being named. A figure with no
    goods named goes to all of the factory's goods. Returns what was saved, in words, for the reply."""
    from app.models import GoodsProfile
    from app.services import goods_profiles

    known = {p.goods_key for p in db.query(GoodsProfile).filter_by(customer_key=key)}
    for record in db.query(InvoiceRecord).filter(InvoiceRecord.status == InvoiceStatus.CONFIRMED):
        order = stock_report.delivery_order(record)
        if order and goods_profiles.customer_key(order) == key:
            known |= {g for row in order["rows"] if (g := goods_profiles.goods_key(row.get("description")))}
    name_words = set(re.findall(r"[a-z]{3,}", name.lower()))
    notes: list[str] = []
    for field, hint, value in figures:
        column = _PROFILE_FIELD.get(field)
        if column is None:
            continue
        words = [w for w in re.findall(r"[a-z]{3,}", hint.lower()) if w not in name_words and w not in _SKIP_WORDS]
        targets = [g for g in known if g in words] or ([words[0]] if words else sorted(known))
        for goods in targets:
            profile = db.query(GoodsProfile).filter_by(customer_key=key, goods_key=goods).first()
            if profile is None:
                profile = GoodsProfile(customer_key=key, goods_key=goods, customer_name=name)
                db.add(profile)
                db.flush()  # the session doesn't autoflush: the next figure for the same goods must find it
            setattr(profile, column, value)
            profile.updated_at = datetime.utcnow()
            notes.append(f"{goods.title()} — {goods_profiles.FIELDS[column]}: {value}")
    db.flush()
    return list(dict.fromkeys(notes))


def save_from_order(db: Session, order: dict) -> tuple[str, list[str]] | None:
    """A letter / email / photo that only gives figures for a known factory's goods: they are kept for its goods.
    Returns (factory name, what was saved), or None when the factory isn't known or nothing could be kept."""
    factory = factory_in_text(db, order.get("customer_name") or "")
    if factory is None:
        return None
    figures = [
        (field, row.get("description") or "", str(row[key]))
        for row in order.get("rows") or []
        for field, key in (("rate", "rate_per_unit"), ("margin", "margin_pct"), ("weight_per_unit", "weight_per_unit"), ("weight_measurement", "weight_measurement"))
        if row.get(key) not in (None, "")
    ]
    notes = save_profile_figures(db, factory[0], factory[1], figures)
    return (factory[1], notes) if notes else None


def announce_profile(db: Session, record: InvoiceRecord, factory: str, notes: list[str]) -> None:
    """Tells the approver what a figures-only email / letter saved."""
    from app.config import settings
    from app.routers import openwa_webhook

    numbers = settings.notify_numbers_list
    if not numbers:
        return
    lines = [f"Saved for {factory} (from #{record.id}):", *(f"• {n}" for n in notes), "", "Used from the next invoice of these goods."]
    openwa_webhook._send_logged(db, numbers[0], "\n".join(lines), record)
    db.commit()


def announce(db: Session, old: InvoiceRecord, new: InvoiceRecord, applied: list[str]) -> None:
    """Tells the approver which earlier order the new letter was added to, and sends its updated stock report."""
    from app.routers import openwa_webhook  # the router imports the pipeline: loaded only when needed

    to_number = old.whatsapp_to_number
    if not to_number:
        logger.warning("#%s has no WhatsApp number to send its updated report to", old.id)
        return
    lines = [f"New letter {new.reference_number or ''} (#{new.id}) has figures for the earlier order #{old.id}:", *(f"• {a}" for a in applied), "", "Making the updated report…"]
    openwa_webhook._send_logged(db, to_number, "\n".join(lines), old)
    db.commit()
    openwa_webhook._send_stock_report(db, old)
    db.commit()
