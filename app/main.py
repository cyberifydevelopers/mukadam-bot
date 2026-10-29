import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app import network, runtime_settings
from app.config import settings
from app.database import get_db, init_db
from app.routers import cad, dashboard_api, gmail_webhook, inventory, invoices, openwa_webhook, stock_templates, whatsapp_webhook
from app.services import file_link
from app.worker import pubsub_listener
from app.worker.scheduler import start_scheduler, stop_scheduler

logging.basicConfig(level=settings.log_level)
network.apply(settings.force_ipv4)

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    runtime_settings.load_overrides()
    pubsub_listener.start()
    start_scheduler(pubsub_active=pubsub_listener.status["state"] == "listening")
    yield
    pubsub_listener.stop()
    stop_scheduler()


app = FastAPI(title="Muccaddam Bot", lifespan=lifespan)

app.include_router(whatsapp_webhook.router)
app.include_router(openwa_webhook.router)
app.include_router(gmail_webhook.router)
app.include_router(invoices.router)
app.include_router(dashboard_api.router)
app.include_router(stock_templates.router)
app.include_router(cad.router)
app.include_router(inventory.router)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/view/{invoice_id}", include_in_schema=False)
def view_file(invoice_id: int, t: str = "", db: Session = Depends(get_db)):
    """The document behind a WhatsApp prompt, opened with its signed link."""
    if not file_link.is_valid(invoice_id, t):
        raise HTTPException(404, "Not found")
    return dashboard_api.invoice_pdf(invoice_id, db)


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(STATIC_DIR / "index.html")
