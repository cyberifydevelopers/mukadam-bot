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
    """Periodic check of Gmail from our own cursor. The main way mail arrives when Pub/Sub isn't running, and the
    safety net when it is: a missed notification must never mean mail waits for the dashboard button. It shares
    sync_gmail()'s lock and skips mail already seen, so it can't process anything twice."""
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


def poll_interval(pubsub_active: bool) -> int:
    """Seconds between Gmail checks (0 = none): the safety-net interval always, and the fallback one as well when
    Pub/Sub isn't running — whichever is shorter."""
    wanted = [settings.gmail_safety_poll_seconds]
    if not pubsub_active:
        wanted.append(settings.gmail_reconcile_interval_seconds)
    wanted = [seconds for seconds in wanted if seconds > 0]
    return min(wanted) if wanted else 0


def start_scheduler(pubsub_active: bool) -> None:
    """New mail arrives through the Pub/Sub listener when it runs (a pull subscription holds notifications while
    the app is down and delivers them on start). Whether or not it does, a periodic check runs too — it is decided
    only once at startup whether Pub/Sub works, and it can fail afterwards."""
    if _scheduler.running:
        return
    seconds = poll_interval(pubsub_active)
    if seconds:
        _scheduler.add_job(_reconcile_job, "interval", seconds=seconds, id="gmail_reconcile")
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
    if not pubsub_active:
        logger.warning("Pub/Sub listener not running — checking Gmail every %ss instead", seconds or "(never: polling is off)")
    else:
        logger.info("New mail via Pub/Sub, with a safety check of Gmail every %ss", seconds or "(off)")


def stop_scheduler() -> None:
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
