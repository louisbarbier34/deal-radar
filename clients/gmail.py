"""
Gmail client — polls inbox for deal-signal keywords.

Auth: OAuth 2.0 (user grants read access once, token is persisted to
GOOGLE_TOKEN_FILE).  Run `python -m clients.gmail --auth` to trigger the
browser flow on first use.
"""
from __future__ import annotations

import base64
import logging
import os
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from googleapiclient.discovery import build

import config
from clients.google_auth import get_credentials

logger = logging.getLogger(__name__)

# Keywords that suggest a deal signal in email content
SIGNAL_KEYWORDS: list[str] = [
    "contract", "agreement", "sow", "statement of work",
    "proposal", "quote", "budget", "scope", "kick-off",
    "kickoff", "production", "invoice", "purchase order",
    "signed", "approved", "green light", "go ahead",
    "awarded", "won", "confirmed",
]

SIGNAL_PATTERN = re.compile(
    "|".join(re.escape(k) for k in SIGNAL_KEYWORDS),
    re.IGNORECASE,
)


class GmailClient:
    def __init__(self) -> None:
        self._service: Any = None

    def _ensure_service(self) -> None:
        if not self._service:
            creds = get_credentials()
            self._service = build("gmail", "v1", credentials=creds)

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    def scan_for_deal_signals(
        self,
        after_timestamp: datetime | None = None,
        max_results: int = 50,
    ) -> list[dict]:
        """
        Scan recent emails for deal-signal keywords.
        Returns list of signal dicts:
          {sender, subject, snippet, date, matched_keywords, thread_id}
        """
        self._ensure_service()
        # Build Gmail search query
        query_parts = [f"({' OR '.join(SIGNAL_KEYWORDS[:10])})"]
        if after_timestamp:
            ts = int(after_timestamp.timestamp())
            query_parts.append(f"after:{ts}")
        # Exclude sent/spam
        query_parts.append("-in:sent -in:spam -in:trash")
        query = " ".join(query_parts)

        results = (
            self._service.users()
            .messages()
            .list(userId="me", q=query, maxResults=max_results)
            .execute()
        )
        messages = results.get("messages", [])
        signals: list[dict] = []

        for msg_ref in messages:
            try:
                msg = (
                    self._service.users()
                    .messages()
                    .get(userId="me", id=msg_ref["id"], format="full")
                    .execute()
                )
                signal = self._extract_signal(msg)
                if signal:
                    signals.append(signal)
            except Exception as exc:
                logger.warning("Failed to fetch message %s: %s", msg_ref["id"], exc)

        return signals

    # ------------------------------------------------------------------ #
    #  Private helpers                                                     #
    # ------------------------------------------------------------------ #

    def _extract_signal(self, msg: dict) -> dict | None:
        headers = {
            h["name"].lower(): h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        subject = headers.get("subject", "")
        sender = headers.get("from", "")
        date_str = headers.get("date", "")
        snippet = msg.get("snippet", "")

        # Parse date
        date: datetime | None = None
        try:
            date = parsedate_to_datetime(date_str).astimezone(timezone.utc)
        except Exception:
            pass

        # Full plain-text body for keyword matching
        body = self._get_plain_text(msg.get("payload", {}))
        combined = f"{subject} {snippet} {body}".lower()

        matched = list({
            kw for kw in SIGNAL_KEYWORDS if kw.lower() in combined
        })
        if not matched:
            return None

        return {
            "thread_id": msg.get("threadId", ""),
            "message_id": msg.get("id", ""),
            "sender": sender,
            "subject": subject,
            "snippet": snippet[:300],
            "date": date,
            "matched_keywords": matched,
            "body_preview": body[:500] if body else snippet[:300],
        }

    def _get_plain_text(self, payload: dict, depth: int = 0) -> str:
        if depth > 5:
            return ""
        mime = payload.get("mimeType", "")
        if mime == "text/plain":
            data = payload.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data + "==").decode(
                    "utf-8", errors="replace"
                )
        for part in payload.get("parts", []):
            result = self._get_plain_text(part, depth + 1)
            if result:
                return result
        return ""


# Singleton
gmail = GmailClient()


# ------------------------------------------------------------------ #
#  CLI auth helper — use clients.google_auth directly instead         #
# ------------------------------------------------------------------ #
if __name__ == "__main__":
    import sys
    print("Run: python -m clients.google_auth")
