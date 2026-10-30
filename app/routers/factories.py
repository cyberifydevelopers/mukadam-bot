"""The Factories tab: each customer (factory) the bot has a delivery order for, and the figures kept for its goods
(margin, weight, rate — see app/services/goods_profiles.py), which can be corrected or cleared here."""

import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import GoodsProfile, InvoiceRecord, InvoiceStatus
from app.services import goods_profiles, stock_report

router = APIRouter(prefix="/api/factories", tags=["factories"])

_NUMBER = re.compile(r"^\d[\d,]*(?:\.\d+)?$")


class ProfileIn(BaseModel):
    margin_pct: str = ""
    weight_per_unit: str = ""
    weight_measurement: str = ""
    rate_per_unit: str = ""


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() + "Z" if dt else None


@router.get("")
def list_factories(db: Session = Depends(get_db)):
    """Every factory with a confirmed delivery order or saved figures, and per goods what is kept for it."""
    factories: dict[str, dict] = {}

    def factory(key: str, name: str) -> dict:
        return factories.setdefault(key, {"key": key, "name": name, "orders": set(), "goods": {}})

    confirmed = db.query(InvoiceRecord).filter(InvoiceRecord.status == InvoiceStatus.CONFIRMED).order_by(InvoiceRecord.id)
    for record in confirmed:
        order = stock_report.delivery_order(record)
        key = goods_profiles.customer_key(order) if order else ""
        if not key:
            continue
        entry = factory(key, stock_report._customer(order))
        entry["name"] = stock_report._customer(order)
        entry["orders"].add(record.id)
        for row in order["rows"]:
            goods = goods_profiles.goods_key(row.get("description"))
            if goods:  # "Putti" and "Putty" are one goods: shown as the first document spelled it
                item = entry["goods"].setdefault(goods, {"key": goods, "orders": set(), "label": goods_profiles.goods_label(row.get("description"))})
                item["orders"].add(record.id)

    for profile in db.query(GoodsProfile).all():
        entry = factory(profile.customer_key, profile.customer_name or profile.customer_key)
        item = entry["goods"].setdefault(profile.goods_key, {"key": profile.goods_key, "orders": set()})
        item["label"] = profile.goods_label or item.get("label")
        item.update({f: getattr(profile, f) for f in goods_profiles.FIELDS}, updated_at=_iso(profile.updated_at), saved=True)

    result = []
    for entry in sorted(factories.values(), key=lambda e: e["name"].lower()):
        goods = []
        for item in sorted(entry["goods"].values(), key=lambda g: g["key"]):
            goods.append({
                "key": item["key"], "label": item.get("label") or item["key"].title(), "saved": item.get("saved", False),
                "updated_at": item.get("updated_at"), "orders": sorted(item["orders"]),
                **{f: item.get(f) for f in goods_profiles.FIELDS},
            })
        result.append({"key": entry["key"], "name": entry["name"], "orders": sorted(entry["orders"]), "goods": goods})
    return {"factories": result}


@router.put("/{customer_key}/{goods_key}")
def save_profile(customer_key: str, goods_key: str, body: ProfileIn, db: Session = Depends(get_db)):
    values = {}
    for field in goods_profiles.FIELDS:
        value = getattr(body, field).strip()
        if value and not _NUMBER.match(value):
            raise HTTPException(422, f"{goods_profiles.FIELDS[field]} must be a number")
        values[field] = value.replace(",", "") or None
    profile = db.query(GoodsProfile).filter_by(customer_key=customer_key, goods_key=goods_key).first()
    if profile is None:
        profile = GoodsProfile(customer_key=customer_key, goods_key=goods_key)
        db.add(profile)
    for field, value in values.items():
        setattr(profile, field, value)
    if not profile.customer_name:
        profile.customer_name = _name_of(db, customer_key)
    profile.updated_at = datetime.utcnow()
    db.commit()
    return {"saved": True}


@router.delete("/{customer_key}/{goods_key}")
def clear_profile(customer_key: str, goods_key: str, db: Session = Depends(get_db)):
    profile = db.query(GoodsProfile).filter_by(customer_key=customer_key, goods_key=goods_key).first()
    if profile is None:
        raise HTTPException(404, "Nothing saved for this")
    db.delete(profile)
    db.commit()
    return {"deleted": True}


def _name_of(db: Session, customer_key: str) -> str | None:
    for record in db.query(InvoiceRecord).filter(InvoiceRecord.status == InvoiceStatus.CONFIRMED).order_by(InvoiceRecord.id.desc()):
        order = stock_report.delivery_order(record)
        if order and goods_profiles.customer_key(order) == customer_key:
            return stock_report._customer(order)
    return None
