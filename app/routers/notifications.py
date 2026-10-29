"""The dashboard's notification bell: what happened lately — new emails, documents saved / skipped / failed,
figures added — built from the records themselves, so nothing is missed while the dashboard was closed.
What counts as read is one timestamp kept in the database (the dashboard has a single user, no login).
Live alerts (WhatsApp disconnected, Gmail not started…) are worked out by the page from /api/status.
"""

import json
from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AppSetting, InvoiceRecord, InvoiceStatus, ProcessedEmail

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

_SEEN_KEY = "notifications_seen_at"
_LIMIT = 40


def _seen(db: Session) -> datetime:
    """When notifications were last marked read; the first time ever, now (the history before that is not news)."""
    row = db.get(AppSetting, _SEEN_KEY)
    if row is None:
        row = AppSetting(key=_SEEN_KEY, value=datetime.utcnow().isoformat())
        db.add(row)
        db.commit()
    return datetime.fromisoformat(row.value)


def _sender(sender: str) -> str:
    name = (sender or "").split("<")[0].strip().strip('"')
    return name or (sender or "someone")


def _file(record: InvoiceRecord) -> str:
    return record.pdf_filename.split("_", 1)[-1]


@router.get("")
def list_notifications(db: Session = Depends(get_db)):
    seen = _seen(db)
    events: list[dict] = []

    for e in db.query(ProcessedEmail).order_by(ProcessedEmail.processed_at.desc()).limit(_LIMIT):
        if e.stage == "failed":
            tone, title = "bad", "Couldn't read an email"
        elif e.classification == "bank":
            tone, title = "info", f"New email from {_sender(e.sender)}"
        elif e.stage in ("reading", "analyzing", "sending"):
            tone, title = "neutral", f"Reading an email from {_sender(e.sender)}…"
        else:
            tone, title = "neutral", f"Email skipped · {_sender(e.sender)}"
        text = e.subject or "(no subject)"
        if e.classification != "bank" and e.classification_reason:
            text += f" — {e.classification_reason}"
        events.append({"id": f"email-{e.id}", "kind": "email", "tone": tone, "title": title, "text": text[:220], "at": e.processed_at, "link": "#emails"})

    wanted = (InvoiceStatus.CONFIRMED, InvoiceStatus.REJECTED, InvoiceStatus.SEND_FAILED, InvoiceStatus.MERGED)
    for r in db.query(InvoiceRecord).filter(InvoiceRecord.status.in_(wanted)).order_by(InvoiceRecord.id.desc()).limit(_LIMIT):
        link = f"#invoice/{r.id}"
        if r.status == InvoiceStatus.CONFIRMED:
            events.append({"id": f"rec-{r.id}-saved", "kind": "saved", "tone": "ok", "title": "Saved to the sheet", "text": f"#{r.id} · {_file(r)}", "at": r.confirmed_at or r.created_at, "link": link})
        elif r.status == InvoiceStatus.REJECTED:
            events.append({"id": f"rec-{r.id}-skipped", "kind": "skipped", "tone": "warn", "title": "Skipped by you", "text": f"#{r.id} · {_file(r)}", "at": r.confirmed_at or r.created_at, "link": link})
        elif r.status == InvoiceStatus.SEND_FAILED:
            events.append({"id": f"rec-{r.id}-send", "kind": "failed", "tone": "bad", "title": "WhatsApp couldn't send an approval request", "text": f"#{r.id} · {_file(r)}", "at": r.created_at, "link": link})
        else:
            extra = json.loads(r.raw_extracted_json or "{}")
            events.append({"id": f"rec-{r.id}-merged", "kind": "figures", "tone": "info", "title": "Figures added", "text": (extra.get("classification_reason") or f"#{r.id} · {_file(r)}")[:220], "at": r.created_at, "link": link})

    events.sort(key=lambda ev: ev["at"] or datetime.min, reverse=True)
    events = events[:_LIMIT]
    for ev in events:
        ev["unread"] = bool(ev["at"] and ev["at"] > seen)
        ev["at"] = ev["at"].isoformat() + "Z" if ev["at"] else None
    return {"events": events, "unread": sum(ev["unread"] for ev in events)}


@router.post("/read")
def mark_read(db: Session = Depends(get_db)):
    row = db.get(AppSetting, _SEEN_KEY)
    now = datetime.utcnow().isoformat()
    if row is None:
        db.add(AppSetting(key=_SEEN_KEY, value=now))
    else:
        row.value = now
    db.commit()
    return {"read": True}
