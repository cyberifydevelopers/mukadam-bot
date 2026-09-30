"""Builds authorized Gmail/Sheets API clients from the OAuth token created by
scripts/gmail_oauth_setup.py.

Both Gmail (read-only) and Sheets (read/write) scopes are requested in the
SAME consent flow / token, since both APIs are used by this app under the
one Google account.
"""

import os

import httplib2
from google.auth.transport.requests import Request
from google_auth_httplib2 import AuthorizedHttp
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from app.config import settings

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


def _load_credentials() -> Credentials:
    if not os.path.exists(settings.google_token_file) and settings.google_token_json:
        # Refreshed tokens are written back to this file below; on a host with
        # an ephemeral disk it's lost on redeploy and re-created from the env
        # var, whose refresh token still works.
        os.makedirs(os.path.dirname(settings.google_token_file) or ".", exist_ok=True)
        with open(settings.google_token_file, "w") as f:
            f.write(settings.google_token_json)

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
    creds = Credentials.from_authorized_user_file(settings.google_token_file)

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(settings.google_token_file, "w") as f:
            f.write(creds.to_json())

    return creds


def has_scope(scope: str) -> bool:
    try:
        return scope in (_load_credentials().scopes or [])
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
    http = AuthorizedHttp(_load_credentials(), http=httplib2.Http(timeout=PUBSUB_HTTP_TIMEOUT))
    return build("pubsub", "v1", http=http, cache_discovery=False)
