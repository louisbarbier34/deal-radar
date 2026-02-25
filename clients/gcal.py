"""
Google Calendar client — detects upcoming prospect/client meetings.

Scans the primary calendar for events that look like sales meetings
(external attendees, deal-related keywords in title) within the next N hours.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from googleapiclient.discovery import build

import config
from clients.google_auth import get_credentials

logger = logging.getLogger(__name__)

# Keywords that suggest a sales / prospect meeting
SALES_KEYWORDS = re.compile(
    r"call|sync|meeting|intro|demo|proposal|pitch|follow.?up|review|check.?in",
    re.IGNORECASE,
)


class GCalClient:
    def __init__(self) -> None:
        self._service: Any = None

    def _ensure_service(self) -> None:
        if not self._service:
            creds = get_credentials()
            self._service = build("calendar", "v3", credentials=creds)

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    def get_upcoming_prospect_meetings(
        self,
        hours_ahead: int = 24,
        notify_before_minutes: int = 60,
    ) -> list[dict]:
        """
        Return meetings starting within `hours_ahead` hours that look like
        prospect/client calls.

        Each result dict:
          {title, start, end, attendees, organizer,
           has_external_attendees, minutes_until_start}
        """
        self._ensure_service()
        now = datetime.now(timezone.utc)
        time_max = now + timedelta(hours=hours_ahead)

        events_result = (
            self._service.events()
            .list(
                calendarId="primary",
                timeMin=now.isoformat(),
                timeMax=time_max.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=50,
            )
            .execute()
        )
        events = events_result.get("items", [])
        upcoming: list[dict] = []

        for event in events:
            parsed = self._parse_event(event, now, notify_before_minutes)
            if parsed and self._looks_like_sales_meeting(parsed):
                upcoming.append(parsed)

        return upcoming

    def get_all_upcoming_meetings(self, days_ahead: int = 7) -> list[dict]:
        """Broader fetch used by A4 calendar nudge logic."""
        self._ensure_service()
        now = datetime.now(timezone.utc)
        time_max = now + timedelta(days=days_ahead)

        events_result = (
            self._service.events()
            .list(
                calendarId="primary",
                timeMin=now.isoformat(),
                timeMax=time_max.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=100,
            )
            .execute()
        )
        return [
            p
            for e in events_result.get("items", [])
            if (p := self._parse_event(e, now)) is not None
        ]

    # ------------------------------------------------------------------ #
    #  Private helpers                                                     #
    # ------------------------------------------------------------------ #

    def _parse_event(
        self,
        event: dict,
        now: datetime,
        notify_before_minutes: int | None = None,
    ) -> dict | None:
        start_raw = event.get("start", {})
        start_str = start_raw.get("dateTime") or start_raw.get("date")
        if not start_str:
            return None

        try:
            if "T" in start_str:
                start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            else:
                # all-day event — skip for nudges
                return None
        except ValueError:
            return None

        end_raw = event.get("end", {})
        end_str = end_raw.get("dateTime") or end_raw.get("date", "")
        end: datetime | None = None
        try:
            end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        except ValueError:
            pass

        attendees = event.get("attendees", [])
        my_email = (config.GOOGLE_ACCOUNT_EMAIL or "").lower()
        external = [
            a["email"]
            for a in attendees
            if a.get("email", "").lower() != my_email
            and not a.get("resource", False)
        ]

        minutes_until = int((start - now).total_seconds() / 60)

        result = {
            "event_id": event.get("id", ""),
            "title": event.get("summary", "(no title)"),
            "start": start,
            "end": end,
            "organizer": event.get("organizer", {}).get("email", ""),
            "attendees": [a.get("email", "") for a in attendees],
            "external_attendees": external,
            "has_external_attendees": bool(external),
            "minutes_until_start": minutes_until,
            "meet_link": event.get("hangoutLink", ""),
            "description": (event.get("description") or "")[:500],
        }

        if notify_before_minutes is not None and minutes_until > notify_before_minutes:
            return None  # Too far away for nudge

        return result

    @staticmethod
    def _looks_like_sales_meeting(event: dict) -> bool:
        """Heuristic: external attendee OR sales keyword in title."""
        if event["has_external_attendees"]:
            return True
        return bool(SALES_KEYWORDS.search(event["title"]))


# Singleton
gcal = GCalClient()
