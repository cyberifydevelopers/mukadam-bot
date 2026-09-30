import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app import network, runtime_settings
from app.config import settings
from app.database import init_db
from app.routers import dashboard_api, gmail_webhook, invoices, openwa_webhook, whatsapp_webhook
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


app = FastAPI(title="Mukadam Bot", lifespan=lifespan)

app.include_router(whatsapp_webhook.router)
app.include_router(openwa_webhook.router)
app.include_router(gmail_webhook.router)
app.include_router(invoices.router)
app.include_router(dashboard_api.router)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(STATIC_DIR / "index.html")
