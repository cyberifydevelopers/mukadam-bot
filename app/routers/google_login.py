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

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from starlette.types import ASGIApp, Receive, Scope, Send

from app import tenancy
from app.config import settings
from app.database import FIRST_USER, SessionLocal, _main_factory, current_user, delete_tenant
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
PUBLIC_PREFIXES = ("/login", "/auth/", "/webhook/", "/view/", "/health", "/about", "/privacy", "/terms")

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
        if user_id is not None:
            with _main_factory() as db:  # a deleted account's cookie must stop working, on any device
                if db.get(User, user_id) is None:
                    user_id = None
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


def _disconnect_google() -> None:
    """Stops the Gmail push and revokes the login, so Google no longer lists the app. Best effort: the account is
    deleted either way."""
    try:
        google_auth.gmail_client().users().stop(userId="me").execute(num_retries=google_auth.GOOGLE_API_RETRIES)
    except Exception:
        logger.warning("Could not stop the Gmail watch while deleting an account", exc_info=True)
    try:
        refresh_token = (json.loads(google_auth._stored_token() or "{}")).get("refresh_token")
        if refresh_token:
            httpx.post("https://oauth2.googleapis.com/revoke", data={"token": refresh_token}, timeout=10)
    except Exception:
        logger.warning("Could not revoke the Google login while deleting an account", exc_info=True)


@router.delete("/api/me")
def delete_me():
    """Deletes the signed-in user: their sign-in, database and files. The first user's is the server's own data
    and stays."""
    user_id = current_user.get()
    if user_id in (None, FIRST_USER):
        return JSONResponse({"detail": "This is the main account of the server and can't be deleted here."}, status_code=400)
    email = connected_email()
    _disconnect_google()
    with tenancy.use(None), SessionLocal() as db:  # first out of the users list, so no background job picks them up again
        db.query(User).filter_by(id=user_id).delete()
        db.commit()
    delete_tenant(user_id)
    logger.info("User %s (id %s) deleted their account", email, user_id)
    response = JSONResponse({"status": "deleted"})
    response.delete_cookie(COOKIE)
    return response


# ---------- sign-in ----------

# Same colours as the dashboard (app/static/index.html), light and dark.
_PAGE_CSS = """
:root{--bg:#f6f7f9;--panel:#fff;--fg:#111827;--muted:#6b7280;--border:#e5e7eb;--accent:#2563eb;--accent-soft:#dbeafe;--bad:#b91c1c;--bad-bg:#fee2e2}
@media(prefers-color-scheme:dark){:root{--bg:#0b0f17;--panel:#131a24;--fg:#e5e7eb;--muted:#94a3b8;--border:#243042;--accent:#3b82f6;--accent-soft:#132646;--bad:#f87171;--bad-bg:#3a1212}}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px 16px;background:var(--bg);color:var(--fg);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.card{width:100%;max-width:400px;background:var(--panel);border:1px solid var(--border);border-radius:16px;padding:36px 32px;text-align:center;
box-shadow:0 10px 30px rgba(15,23,42,.08)}
.logo{width:52px;height:52px;margin:0 auto 18px;border-radius:14px;background:var(--accent);color:#fff;display:flex;align-items:center;justify-content:center;
font-weight:700;font-size:22px}
h1{margin:0 0 6px;font-size:22px;letter-spacing:-.2px}
.sub{margin:0 0 24px;color:var(--muted)}
.btn{display:flex;align-items:center;justify-content:center;gap:12px;width:100%;padding:12px 18px;border-radius:10px;border:1px solid var(--border);
background:#fff;color:#1f2937;font-weight:600;font-size:15px;text-decoration:none;transition:box-shadow .15s,border-color .15s}
.btn:hover{border-color:var(--accent);box-shadow:0 2px 10px rgba(37,99,235,.18)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.note{margin:14px 0 0;font-size:13px;color:var(--muted)}
.points{list-style:none;margin:26px 0 0;padding:20px 0 0;border-top:1px solid var(--border);text-align:left;font-size:13.5px;color:var(--muted)}
.points li{display:flex;gap:10px;margin:0 0 10px}
.points li:last-child{margin:0}
.points b{color:var(--accent);flex:none}
.alert{margin:0 0 20px;padding:10px 14px;border-radius:10px;background:var(--bad-bg);color:var(--bad);font-size:14px;text-align:left}
.msg{margin:0 0 24px;color:var(--muted);text-align:left}
.foot{margin:22px 0 0;font-size:12.5px;color:var(--muted)}
.foot a,.card.wide a:not(.btn){color:var(--accent)}
.card.wide{max-width:720px;text-align:left}
.card.wide .logo{margin-left:0}
.card.wide h2{margin:26px 0 6px;font-size:16px}
.card.wide p,.card.wide li{color:var(--muted);margin:0 0 10px}
.card.wide ul{padding-left:20px;margin:0 0 10px}
.card.wide .btn{display:inline-flex;width:auto}
"""

_GOOGLE_G = (
    "<svg width='20' height='20' viewBox='0 0 48 48' aria-hidden='true'>"
    "<path fill='#EA4335' d='M24 9.5c3.5 0 6.6 1.2 9.1 3.6l6.8-6.8C35.8 2.4 30.3 0 24 0 14.6 0 6.5 5.4 2.6 13.2l7.9 6.1C12.4 13.6 17.7 9.5 24 9.5z'/>"
    "<path fill='#4285F4' d='M46.1 24.5c0-1.6-.1-3.1-.4-4.5H24v9h12.4c-.5 2.9-2.1 5.3-4.6 7l7.1 5.5c4.2-3.8 6.7-9.5 6.7-17z'/>"
    "<path fill='#FBBC05' d='M10.5 28.7a14.5 14.5 0 0 1 0-9.4l-7.9-6.1a24 24 0 0 0 0 21.6l7.9-6.1z'/>"
    "<path fill='#34A853' d='M24 48c6.5 0 11.9-2.1 15.9-5.8l-7.1-5.5c-2 1.4-4.6 2.3-8.8 2.3-6.3 0-11.6-4.1-13.5-9.8l-7.9 6.1C6.5 42.6 14.6 48 24 48z'/>"
    "</svg>"
)


def _shell(title: str, body: str, status: int = 200, wide: bool = False) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{escape(title)} · Muccaddam Bot</title><style>{_PAGE_CSS}</style>"
        f"<body><main class='card{' wide' if wide else ''}'><div class=logo>M</div>{body}</main></body></html>",
        status_code=status,
    )


def _page(title: str, text: str, link: str = "/login", link_text: str = "Back to sign in", status: int = 400) -> HTMLResponse:
    return _shell(
        title,
        f"<h1>{escape(title)}</h1><p class=msg>{escape(text)}</p><a class='btn primary' href='{escape(link)}'>{escape(link_text)}</a>",
        status,
    )


_POINTS = (
    "Your bank emails are read automatically and sent to WhatsApp for a YES / NO.",
    "Read-only access to your Gmail. Nothing is ever sent, changed or deleted.",
    "Everyone sees only their own dashboard.",
)


@router.get("/login", include_in_schema=False)
def login_page(request: Request, error: str = ""):
    if _user_from_cookie(request.cookies.get(COOKIE, "")) is not None:
        return RedirectResponse("/")
    button = (
        f"<a class=btn href='/auth/google/start'>{_GOOGLE_G}<span>Continue with Google</span></a>"
        "<p class=note>New here? Signing in with Google creates your dashboard automatically.</p>"
        if is_available()
        else "<div class=alert>Google sign-in isn't set up yet (the OAuth client is missing).</div>"
    )
    alert = f"<div class=alert>{escape(error)}</div>" if error else ""
    points = "".join(f"<li><b>✓</b><span>{escape(p)}</span></li>" for p in _POINTS)
    return _shell(
        "Sign in",
        f"<h1>Welcome to Muccaddam Bot</h1><p class=sub>Sign in with the Gmail account whose bank emails you want to track.</p>"
        f"{alert}{button}<ul class=points>{points}</ul>"
        "<p class=foot><a href='/about'>About</a> · <a href='/privacy'>Privacy Policy</a> · <a href='/terms'>Terms of Service</a></p>",
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
