"""Near-real-time Gmail ingestion WITHOUT a public URL: a background thread
that pulls Gmail's new-mail notifications from a Pub/Sub PULL subscription
(GOOGLE_PUBSUB_SUBSCRIPTION) and runs sync_gmail() for them.

Gmail's users.watch() publishes to the topic on every mailbox change; a push
subscription would need Google to reach this app over HTTPS (the
/webhook/gmail route), a pull subscription only needs this app to reach
Google. Each notification only says "something changed" — sync_gmail()
works out what from its own stored cursor — so a batch of notifications is
handled with a single sync, and acknowledged only once that sync succeeded
(an unacknowledged message is redelivered by Pub/Sub, so a failed sync is
retried rather than lost). While this listener runs there is no polling
timer (app/worker/scheduler.py); a pull subscription holds notifications
while the app is down, so nothing is missed.

Uses the Pub/Sub REST API through the same OAuth token as Gmail/Sheets,
which must include the pubsub scope (re-run scripts/gmail_oauth_setup.py
once after upgrading).
"""

import logging
import threading
from datetime import datetime

from googleapiclient.errors import HttpError

from app.config import settings
from app.database import SessionLocal
from app.pipeline import sync_gmail
from app.services.google_auth import GOOGLE_API_RETRIES, PUBSUB_SCOPE, has_scope, pubsub_client

logger = logging.getLogger(__name__)

_stop = threading.Event()
_thread: threading.Thread | None = None

# Health for the dashboard: "disabled" / "needs_auth" / "listening" / "error"
status: dict = {"state": "disabled", "detail": None, "last_message_at": None}


def subscription_path() -> str:
    sub = settings.google_pubsub_subscription.strip()
    if not sub or sub.startswith("projects/"):
        return sub
    # Short name: take the project from the topic (projects/<id>/topics/<name>).
    project = settings.google_pubsub_topic.split("/")[1] if settings.google_pubsub_topic.startswith("projects/") else ""
    return f"projects/{project}/subscriptions/{sub}" if project else sub


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _set(state: str, detail: str | None = None) -> None:
    status["state"], status["detail"] = state, detail


def _run() -> None:
    subscription = subscription_path()
    service = None
    while not _stop.is_set():
        try:
            if service is None:
                service = pubsub_client()
            # Long poll: the call waits (up to ~a minute) for messages to arrive.
            resp = (
                service.projects()
                .subscriptions()
                .pull(subscription=subscription, body={"maxMessages": 50})
                .execute(num_retries=GOOGLE_API_RETRIES)
            )
            _set("listening")
        except HttpError as exc:
            code = exc.resp.status
            if code in (403, 404):
                # Wrong name / no permission won't fix itself quickly — say so and back off.
                _set("error", f"HTTP {code} pulling {subscription}: {exc.reason}")
                logger.error("Pub/Sub pull failed (%s) for %s: %s", code, subscription, exc)
                _stop.wait(120)
            else:
                _set("error", f"HTTP {code}: {exc.reason}")
                _stop.wait(10)
            continue
        except Exception as exc:  # network drops, token refresh failures
            _set("error", f"{type(exc).__name__}: {exc}")
            logger.warning("Pub/Sub pull failed: %s", exc)
            service = None  # rebuild the client/connection on the next try
            _stop.wait(10)
            continue

        messages = resp.get("receivedMessages", [])
        if not messages:
            continue

        logger.info("Pub/Sub: %d Gmail notification(s) — syncing", len(messages))
        received_at = datetime.utcnow()
        status["last_message_at"] = received_at.isoformat() + "Z"
        # publishTime: when Google published the notification (RFC 3339, UTC).
        # The newest notification in the batch is the one for the newest mail.
        published_at = max(_parse_time(m["message"].get("publishTime")) or received_at for m in messages)
        status["last_published_at"] = published_at.isoformat() + "Z"
        status["last_delivery_seconds"] = round((received_at - published_at).total_seconds(), 1)
        logger.info(
            "Pub/Sub notification published %s, received %.1fs later",
            published_at.isoformat(), (received_at - published_at).total_seconds(),
        )
        ack_ids = [m["ackId"] for m in messages]
        try:
            # The subscription's default ack deadline (often 10s) is shorter
            # than classifying + extracting a scanned PDF with the LLM; extend
            # it so Pub/Sub doesn't redeliver while the sync is still running.
            service.projects().subscriptions().modifyAckDeadline(
                subscription=subscription, body={"ackIds": ack_ids, "ackDeadlineSeconds": 600}
            ).execute(num_retries=GOOGLE_API_RETRIES)
        except Exception as exc:
            logger.warning("Couldn't extend Pub/Sub ack deadline (a redelivery may trigger one extra sync): %s", exc)
        db = SessionLocal()
        try:
            created = sync_gmail(db, notification=(published_at, received_at))
            if created:
                logger.info("Pub/Sub-triggered sync queued %d new item(s)", created)
        except Exception:
            # Not acknowledged → Pub/Sub redelivers after the ack deadline and
            # the sync is retried then.
            logger.exception("Sync after Pub/Sub notification failed; will retry on redelivery")
            db.rollback()
            continue
        finally:
            db.close()

        try:
            service.projects().subscriptions().acknowledge(
                subscription=subscription, body={"ackIds": ack_ids}
            ).execute(num_retries=GOOGLE_API_RETRIES)
        except Exception as exc:
            # Harmless: redelivery just triggers one more (no-op) sync.
            logger.warning("Pub/Sub acknowledge failed: %s", exc)


def start() -> None:
    global _thread
    if not settings.google_pubsub_subscription:
        _set("disabled", "GOOGLE_PUBSUB_SUBSCRIPTION not set — relying on the periodic check")
        logger.info("Pub/Sub listener off: GOOGLE_PUBSUB_SUBSCRIPTION not set")
        return
    if not has_scope(PUBSUB_SCOPE):
        _set("needs_auth", "Google login lacks Pub/Sub permission — run: python scripts/gmail_oauth_setup.py")
        logger.warning(
            "Pub/Sub listener off: credentials/token.json has no pubsub scope. "
            "Run `python scripts/gmail_oauth_setup.py` once, then restart."
        )
        return
    _stop.clear()
    _thread = threading.Thread(target=_run, name="pubsub-listener", daemon=True)
    _thread.start()
    _set("listening")
    logger.info("Pub/Sub listener started on %s", subscription_path())


def stop() -> None:
    _stop.set()
