"""Run this ONCE, locally, with a browser available, to authorize this app
against your Gmail + Google Sheets account and create credentials/token.json.

Prerequisites (see docs/WORKFLOW.md "Google API setup" for the exact
Console steps):
  1. A Google Cloud project with the Gmail API and Google Sheets API enabled.
  2. An OAuth consent screen configured (type: External, since this is a
     personal Gmail account), with your own Gmail address added as a test
     user.
  3. An OAuth client ID of type "Desktop app", downloaded as
     credentials/client_secret.json (path configurable via
     GOOGLE_CLIENT_SECRETS_FILE in .env).

Usage:
    python scripts/gmail_oauth_setup.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402

from app.config import settings  # noqa: E402
from app.services.google_auth import SCOPES  # noqa: E402


def main() -> None:
    flow = InstalledAppFlow.from_client_secrets_file(settings.google_client_secrets_file, SCOPES)
    creds = flow.run_local_server(port=0)

    token_path = Path(settings.google_token_file)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())

    print(f"Saved credentials to {token_path}")


if __name__ == "__main__":
    main()
