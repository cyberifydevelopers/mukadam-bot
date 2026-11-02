""""Continue with Google": how people sign in. Each person who does gets their own dashboard: their Gmail is read
with their own login, and their emails, invoices, settings and files are kept apart from everybody else's
(app/database.py, app/tenancy.py).

The session is a signed cookie holding the user's id. SignedInMiddleware turns it into the user the rest of the
request works for, and sends anybody not signed in to /login.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from html import escape

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from starlette.types import ASGIApp, Receive, Scope, Send

from app import tenancy
from app.config import settings
from app.database import FIRST_USER, SessionLocal, current_user
from app.models import GmailSyncState, User
from app.pipeline import _SYNC_LOCK, _get_or_init_sync_state
from app.services import google_auth
from app.services.file_link import base_url
from app.worker import pubsub_listener

logger = logging.getLogger(__name__)

router = APIRouter(tags=["google-login"])

COOKIE = "mukadam_session"
COOKIE_SECONDS = 30 * 24 * 3600

# Open to anybody, signed in or not: the sign-in itself, the webhooks (their own checks), the signed "view file" link.
PUBLIC_PREFIXES = ("/login", "/auth/", "/webhook/", "/view/", "/health")

# state -> (PKCE code verifier, time started), for the few minutes between the redirect to Google and back.
_pending: dict[str, tuple[str, float]] = {}
_PENDING_SECONDS = 600

# The login may give fewer scopes than asked (a box left unticked): checked below with a clear message, rather
# than oauthlib raising on the difference.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
# Google redirects back to http://localhost when run on your own computer.
if base_url().startswith("http://"):
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")


def _client_config() -> dict | None:
    raw = settings.google_client_secrets_json.strip()
    if not raw and os.path.exists(settings.google_client_secrets_file):
        with open(settings.google_client_secrets_file) as f:
            raw = f.read()
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def is_available() -> bool:
    return _client_config() is not None


def _redirect_uri(request: Request) -> str:
    return f"{base_url() or str(request.base_url).rstrip('/')}/auth/google/callback"


# ---------- the session cookie ----------

def _key() -> bytes:
    if settings.session_secret:
        return settings.session_secret.encode()
    config = _client_config() or {}
    client = config.get("web") or config.get("installed") or {}
    return hashlib.sha256(f"session:{client.get('client_secret', '')}".encode()).digest()


def _sign(text: str) -> str:
    return hmac.new(_key(), text.encode(), hashlib.sha256).hexdigest()


def _make_cookie(user_id: int) -> str:
    body = f"{user_id}.{int(time.time()) + COOKIE_SECONDS}"
    return f"{body}.{_sign(body)}"


def _user_from_cookie(value: str) -> int | None:
    try:
        user_id, expires, signature = value.split(".")
        if hmac.compare_digest(_sign(f"{user_id}.{expires}"), signature) and int(expires) > time.time():
            return int(user_id)
    except ValueError:
        pass
    return None


def _cookie_from_scope(scope: Scope) -> str:
    for name, value in scope.get("headers", []):
        if name == b"cookie":
            for part in value.decode("latin-1").split(";"):
                key, _, val = part.strip().partition("=")
                if key == COOKIE:
                    return val
    return ""


def _set_cookie(response, user_id: int) -> None:
    response.set_cookie(COOKIE, _make_cookie(user_id), max_age=COOKIE_SECONDS, httponly=True, samesite="lax", secure=base_url().startswith("https://"))


class SignedInMiddleware:
    """Everything that isn't public needs a signed-in user, and then runs as that user."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].startswith(PUBLIC_PREFIXES):
            return await self.app(scope, receive, send)
        user_id = _user_from_cookie(_cookie_from_scope(scope))
        if user_id is None:
            if scope["path"].startswith(("/api/", "/invoices")):
                response = JSONResponse({"detail": "Not signed in"}, status_code=401)
            else:
                response = RedirectResponse("/login")
            return await response(scope, receive, send)
        token = current_user.set(user_id)
        try:
            await self.app(scope, receive, send)
        finally:
            current_user.reset(token)


# ---------- the signed-in user ----------

def connected_email() -> str | None:
    user_id = current_user.get()
    if user_id is None:
        return None
    with tenancy.use(None), SessionLocal() as db:  # the users list is in the first database
        user = db.get(User, user_id)
    return user.email if user else None


@router.get("/api/me")
def me():
    return {"email": connected_email()}


# ---------- sign-in ----------

def _page(title: str, text: str, link: str = "/login", link_text: str = "Back to sign in", status: int = 400) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{escape(title)}</title>"
        f"<body style='font:16px system-ui;max-width:480px;margin:15vh auto;padding:0 16px'>"
        f"<h2>{escape(title)}</h2><p>{escape(text)}</p><p><a href='{escape(link)}'>{escape(link_text)}</a></p></body>",
        status_code=status,
    )


@router.get("/login", include_in_schema=False)
def login_page(request: Request):
    if _user_from_cookie(request.cookies.get(COOKIE, "")) is not None:
        return RedirectResponse("/")
    button = (
        "<a href='/auth/google/start' style='display:inline-block;padding:12px 22px;background:#1a73e8;color:#fff;"
        "border-radius:8px;text-decoration:none;font-weight:600'>Continue with Google</a>"
        if is_available()
        else "<p>Google sign-in isn't set up yet (the OAuth client is missing).</p>"
    )
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>Sign in</title><body style='font:16px system-ui;max-width:420px;margin:18vh auto;padding:0 16px;text-align:center'>"
        "<h2>Muccaddam Bot</h2><p style='color:#666'>Sign in with the Gmail account whose bank emails you want to track. "
        "Everyone sees only their own dashboard.</p>"
        f"<p style='margin-top:28px'>{button}</p></body>"
    )


@router.get("/auth/google/start", include_in_schema=False)
def start(request: Request):
    config = _client_config()
    if config is None:
        return _page("Google sign-in isn't set up", "Add the OAuth client (client_secret.json, or GOOGLE_CLIENT_SECRETS_JSON).")
    now = time.time()
    for state in [k for k, (_, t) in _pending.items() if now - t > _PENDING_SECONDS]:
        del _pending[state]

    flow = Flow.from_client_config(config, google_auth.LOGIN_SCOPES, redirect_uri=_redirect_uri(request))
    state = secrets.token_urlsafe(24)
    # offline + consent: Google only gives a refresh token (what keeps the bot reading the mail) when asked for both.
    url, _ = flow.authorization_url(access_type="offline", prompt="consent select_account", state=state)
    _pending[state] = (flow.code_verifier, now)
    return RedirectResponse(url)


def _gmail_address(creds: Credentials) -> str:
    profile = build("gmail", "v1", credentials=creds, cache_discovery=False).users().getProfile(userId="me").execute(
        num_retries=google_auth.GOOGLE_API_RETRIES
    )
    return profile["emailAddress"].lower()


def _forget_cursor_of_other_mailbox(email: str) -> None:
    """The first user's data and Gmail cursor come from the mailbox set up in .env. Signing in with another Gmail
    there starts from now in the new one."""
    try:
        old = _gmail_address(google_auth._load_app_credentials())
    except Exception:
        old = None
    if old != email:
        with SessionLocal() as db:
            db.query(GmailSyncState).delete()
            db.commit()


@router.get("/auth/google/callback", include_in_schema=False)
def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        return _page("Google sign-in cancelled", error)
    pending = _pending.pop(state, None)
    if not code or pending is None or time.time() - pending[1] > _PENDING_SECONDS:
        return _page("Google sign-in expired", "Please start again with Continue with Google.")

    flow = Flow.from_client_config(_client_config(), google_auth.LOGIN_SCOPES, redirect_uri=_redirect_uri(request))
    flow.code_verifier = pending[0]
    try:
        flow.fetch_token(code=code)
        creds = flow.credentials
        if set(google_auth.LOGIN_SCOPES) - set(creds.scopes or []):
            return _page("Permission missing", "Reading your Gmail is needed. Sign in again and tick the box.")
        if not creds.refresh_token:
            return _page("No lasting login", "Google didn't give a refresh token. Remove the app at myaccount.google.com/permissions and try again.")
        email = _gmail_address(creds)
    except Exception as exc:
        logger.exception("Google sign-in failed")
        return _page("Google sign-in failed", str(exc))

    allowed = [e.strip().lower() for e in settings.allowed_emails.split(",") if e.strip()]
    if allowed and email not in allowed:
        return _page("Not allowed", f"{email} is not on the list of people who can use this dashboard.")

    with tenancy.use(None), SessionLocal() as db:
        user, created = tenancy.find_or_create_user(db, email)
        user_id = user.id

    problem = None
    with tenancy.use(user_id):
        google_auth.save_token(creds.to_json())
        if created:
            logger.info("New user %s signed in (id %s)", email, user_id)
            with _SYNC_LOCK, SessionLocal() as db:
                if user_id == FIRST_USER:
                    _forget_cursor_of_other_mailbox(email)
                try:
                    _get_or_init_sync_state(db)
                    db.commit()
                except Exception as exc:
                    db.rollback()
                    logger.exception("Could not start the Gmail watch for %s", email)
                    problem = str(exc)
    pubsub_listener.start()

    response = (
        _page("Signed in, with a problem", f"Gmail push could not be started for {email}: {problem}. New mail is still checked every few minutes.", "/", "Open dashboard", 200)
        if problem
        else RedirectResponse("/")
    )
    _set_cookie(response, user_id)
    return response


@router.get("/auth/logout", include_in_schema=False)
def logout():
    response = RedirectResponse("/login")
    response.delete_cookie(COOKIE)
    return response
