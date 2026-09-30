import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import settings
from app.database import SessionLocal
from app.models import GmailSyncState
from app.pipeline import sync_gmail
from app.services import gmail_service

logger = logging.getLogger(__name__)

_scheduler = BackgroundScheduler()


def _reconcile_job() -> None:
    """Fallback polling, used ONLY when the Pub/Sub listener isn't running
    (no subscription configured / no permission): without it new mail would
    never be picked up at all."""
    db = SessionLocal()
    try:
        created = sync_gmail(db)
        if created:
            logger.info("Reconciliation sync queued %d new invoice(s)", created)
    except Exception:
        logger.exception("Reconciliation sync failed")
    finally:
        db.close()


def _renew_watch_job() -> None:
    """Gmail caps a watch() at ~7 days; renewing well before that keeps push
    notifications flowing without a gap."""
    db = SessionLocal()
    try:
        history_id, expiration_ms = gmail_service.start_watch()
        state = db.get(GmailSyncState, 1)
        if state is None:
            state = GmailSyncState(id=1, last_history_id=history_id)
            db.add(state)
        state.watch_expiration_ms = expiration_ms
        db.commit()
        logger.info("Renewed Gmail watch, expires at epoch ms %s", expiration_ms)
    except Exception:
        logger.exception("Gmail watch renewal failed")
    finally:
        db.close()


def start_scheduler(pubsub_active: bool) -> None:
    """With the Pub/Sub listener running, new mail arrives through it alone —
    no timer. (A pull subscription holds notifications while the app is
    down and delivers them on start, so nothing is missed.) The polling
    job is only a fallback for when Pub/Sub isn't available."""
    if _scheduler.running:
        return
    poll = not pubsub_active and settings.gmail_reconcile_interval_seconds > 0
    if poll:
        _scheduler.add_job(
            _reconcile_job,
            "interval",
            seconds=settings.gmail_reconcile_interval_seconds,
            id="gmail_reconcile",
        )
    # Keeps Gmail publishing to the topic at all — needed with or without polling.
    # First run right at startup, not one interval later: on a fresh database
    # (e.g. a redeploy on a host with an ephemeral disk) there's no watch or
    # sync baseline until this runs.
    _scheduler.add_job(
        _renew_watch_job,
        "interval",
        seconds=settings.gmail_watch_renew_interval_seconds,
        id="gmail_watch_renew",
        next_run_time=datetime.now(),
    )
    _scheduler.start()
    if poll:
        logger.warning(
            "Pub/Sub listener not running — falling back to checking Gmail every %ss",
            settings.gmail_reconcile_interval_seconds,
        )
    else:
        logger.info("New mail via Pub/Sub only (no polling timer)")


def stop_scheduler() -> None:
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
