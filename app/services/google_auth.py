"""Builds authorized Gmail/Sheets API clients from the OAuth token created by
scripts/gmail_oauth_setup.py.

Both Gmail (read-only) and Sheets (read/write) scopes are requested in the
SAME consent flow / token, since both APIs are used by this app under the
one Google account.
"""

import json
import os

import httplib2
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google_auth_httplib2 import AuthorizedHttp
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from app import tenancy
from app.config import settings
from app.database import SessionLocal
from app.models import AppSetting

PUBSUB_SCOPE = "https://www.googleapis.com/auth/pubsub"

# Requested by scripts/gmail_oauth_setup.py. Pub/Sub is for pulling Gmail's
# new-mail notifications from GOOGLE_PUBSUB_SUBSCRIPTION (app/worker/pubsub_listener.py).
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    PUBSUB_SCOPE,
]


# Passed as `.execute(num_retries=...)`: the client then retries dropped
# connections (SSL EOF, resets, timeouts) and 5xx/429 with backoff. Not used
# for the Sheets row append, where a retry after a lost response would
# write the row twice.
GOOGLE_API_RETRIES = 3


class GoogleAuthError(Exception):
    pass


# What people sign in for with "Continue with Google": reading their mail. The login is kept in their own database
# (it survives a redeploy, where a token file on the host's disk would not).
LOGIN_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_KEY = "google_token_json"


def _stored_token() -> str:
    try:
        with SessionLocal() as db:
            row = db.get(AppSetting, TOKEN_KEY)
            return row.value if row else ""
    except Exception:  # no table yet (before init_db) or db down: fall back to the file
        return ""


def save_token(token_json: str) -> None:
    with SessionLocal() as db:
        row = db.get(AppSetting, TOKEN_KEY)
        if row is None:
            db.add(AppSetting(key=TOKEN_KEY, value=token_json))
        else:
            row.value = token_json
        db.commit()


def _token_file_usable() -> bool:
    path = settings.google_token_file
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return False
    try:
        with open(path) as f:
            json.load(f)
        return True
    except ValueError:
        return False


def _load_credentials() -> Credentials:
    """The Google login of the user the code is working for. The first user's mailbox set up in .env
    (token.json / GOOGLE_TOKEN_JSON) is used until they sign in."""
    stored = _stored_token()
    if stored:
        try:
            creds = Credentials.from_authorized_user_info(json.loads(stored))
        except ValueError as exc:
            raise GoogleAuthError(f"The saved Google login is not valid ({exc}). Sign in with Google again.") from exc
        return _refreshed(creds, lambda: save_token(creds.to_json()))
    if not tenancy.is_first_user():
        raise GoogleAuthError("Google is not connected. Sign in with Google again.")
    return _load_app_credentials()


def _load_app_credentials() -> Credentials:
    """The login from .env: the first user's Gmail before they sign in, and what pulls Pub/Sub for everybody."""
    token_json = settings.google_token_json.strip()
    if token_json and not _token_file_usable():
        # Refreshed tokens are written back to this file below; on a host with
        # an ephemeral disk it's lost on redeploy and re-created from the env
        # var, whose refresh token still works.
        os.makedirs(os.path.dirname(settings.google_token_file) or ".", exist_ok=True)
        with open(settings.google_token_file, "w") as f:
            f.write(token_json)

    if not os.path.exists(settings.google_token_file):
        raise GoogleAuthError(
            f"{settings.google_token_file} not found. Run "
            "`python scripts/gmail_oauth_setup.py` once, locally with a browser "
            "available, to complete the OAuth consent flow and create it "
            "(or set GOOGLE_TOKEN_JSON to its contents)."
        )

    # Use the scopes the token was actually granted (saved in the file), not
    # SCOPES: refreshing a token while asking for a scope it never got fails
    # with invalid_scope — which would break Gmail too, not just Pub/Sub, for
    # a token created before a scope was added here.
    try:
        creds = Credentials.from_authorized_user_file(settings.google_token_file)
    except ValueError as exc:
        # Not raised as-is: a bad token must not crash app startup
        # (pubsub_listener.start -> has_scope only catches GoogleAuthError).
        raise GoogleAuthError(
            f"{settings.google_token_file} is not a valid token.json ({exc}). "
            f"GOOGLE_TOKEN_JSON is {'set, ' + str(len(token_json)) + ' chars' if token_json else 'empty'} "
            "— it must hold the whole token.json, from { to }."
        ) from exc

    def write_file() -> None:
        with open(settings.google_token_file, "w") as f:
            f.write(creds.to_json())

    return _refreshed(creds, write_file)


def _refreshed(creds: Credentials, save) -> Credentials:
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            # Raised as GoogleAuthError so an expired login can't crash app startup (see has_scope).
            raise GoogleAuthError(
                f"Google login expired or was revoked ({exc}). Sign in with Google again "
                "(for the Gmail set up in .env: `python scripts/gmail_oauth_setup.py`)."
            ) from exc
        save()
    return creds


def has_scope(scope: str) -> bool:
    try:
        return scope in (_load_app_credentials().scopes or [])
    except GoogleAuthError:
        return False


def gmail_client():
    return build("gmail", "v1", credentials=_load_credentials(), cache_discovery=False)


def sheets_client():
    return build("sheets", "v4", credentials=_load_credentials(), cache_discovery=False)


# Pub/Sub pulls are long polls: without a socket timeout, a connection the
# network silently dropped (common here — see the SSL EOF errors) hangs the
# listener for minutes and new-mail notifications sit unread. 90 s is longer
# than a long poll's own wait, so only dead connections hit it.
PUBSUB_HTTP_TIMEOUT = 90


def pubsub_client():
    # One subscription serves everybody, so it is read with the login from .env, not a person's own.
    http = AuthorizedHttp(_load_app_credentials(), http=httplib2.Http(timeout=PUBSUB_HTTP_TIMEOUT))
    return build("pubsub", "v1", http=http, cache_discovery=False)
