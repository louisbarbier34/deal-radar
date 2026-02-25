"""
Unified Google OAuth credential manager.
Shared by clients/gmail.py and clients/gcal.py.

Combined scopes cover both Gmail (readonly) and Calendar (readonly).
One browser auth prompt, one token file, no scope conflicts.

Run once to authenticate:
  python -m clients.google_auth
"""
from __future__ import annotations

import os
import logging

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

import config

logger = logging.getLogger(__name__)

# Combined scopes for all Google services Viktor uses
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]


def get_credentials() -> Credentials:
    """
    Return valid Google credentials, refreshing or re-authorising as needed.
    Token is cached to GOOGLE_TOKEN_FILE.
    """
    creds: Credentials | None = None
    token_path = config.GOOGLE_TOKEN_FILE
    creds_path = config.GOOGLE_CREDENTIALS_FILE

    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Google token expired — refreshing…")
            creds.refresh(Request())
        else:
            if not os.path.exists(creds_path):
                raise FileNotFoundError(
                    f"Google credentials file not found: {creds_path}\n"
                    "Download it from Google Cloud Console → APIs & Services → Credentials\n"
                    "Enable Gmail API and Google Calendar API first."
                )
            logger.info("Opening browser for Google OAuth…")
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(token_path, "w") as f:
            f.write(creds.to_json())
        logger.info("Google credentials saved to %s", token_path)

    return creds


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    get_credentials()
    print(f"Google auth complete. Token saved to {config.GOOGLE_TOKEN_FILE}")
