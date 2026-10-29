"""The CAD tab: the officers / bank people whose emails the bot entertains
(see pipeline._sender_not_allowed)."""

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import CadContact

router = APIRouter(prefix="/api/cad", tags=["cad"])

_ADDRESS = re.compile(r"^(?:[\w.+'-]+)?@[\w-]+(?:\.[\w-]+)+$")


class ContactIn(BaseModel):
    address: str
    name: str = ""


def _json(c: CadContact) -> dict:
    return {
        "id": c.id,
        "address": c.address,
        "name": c.name,
        "is_domain": c.address.startswith("@"),
        "added_at": c.created_at.isoformat() + "Z",
    }


@router.get("")
def list_contacts(db: Session = Depends(get_db)):
    contacts = db.query(CadContact).order_by(CadContact.created_at.desc()).all()
    return {"contacts": [_json(c) for c in contacts]}


@router.post("")
def add_contact(body: ContactIn, db: Session = Depends(get_db)):
    address = body.address.strip().lower()
    if not _ADDRESS.match(address):
        raise HTTPException(422, "Enter an email address (name@bank.com) or a whole domain (@bank.com)")
    if db.query(CadContact).filter_by(address=address).first():
        raise HTTPException(409, f"{address} is already in the list")
    contact = CadContact(address=address, name=body.name.strip() or None)
    db.add(contact)
    db.commit()
    return _json(contact)


@router.delete("/{contact_id}")
def delete_contact(contact_id: int, db: Session = Depends(get_db)):
    contact = db.get(CadContact, contact_id)
    if not contact:
        raise HTTPException(404, "Not found")
    db.delete(contact)
    db.commit()
    return {"deleted": contact_id}
