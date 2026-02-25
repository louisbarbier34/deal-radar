"""
A4 — Calendar Pre-Meeting Nudge
Runs every 30 min. If a prospect/client meeting starts within 60 min,
DMs the deal owner with a research brief from the Research Agent.

Now backed by the Research Agent, which:
  - Looks up existing deal history in Attio
  - Web-searches recent company news / campaigns
  - Returns a concise pre-meeting brief with smart conversation angles
"""
from __future__ import annotations

import logging

from slack_sdk.web.async_client import AsyncWebClient

from clients.attio import attio, AttioClient
from clients.gcal import gcal
from clients.state import state
from agents.research_agent import run_research_agent

logger = logging.getLogger(__name__)

_NS = "a4_nudge"


async def run_calendar_nudge(slack_client: AsyncWebClient) -> None:
    """Check for upcoming meetings and DM deal owners with a research brief."""
    try:
        meetings = gcal.get_upcoming_prospect_meetings(
            hours_ahead=2, notify_before_minutes=60
        )
    except Exception as exc:
        logger.error("A4: Calendar fetch failed: %s", exc)
        return

    for meeting in meetings:
        event_id = meeting["event_id"]
        if state.has_processed(_NS, event_id):
            continue

        await _send_nudge(meeting, slack_client)
        state.mark_processed(_NS, event_id)


async def _send_nudge(meeting: dict, slack_client: AsyncWebClient) -> None:
    title = meeting["title"]
    start = meeting["start"]
    minutes = meeting["minutes_until_start"]
    external = meeting["external_attendees"]

    # Infer company name from meeting title or attendee domain
    company_name = _extract_company(title, external)

    time_str = start.strftime("%I:%M %p")
    mins_text = f"{minutes} min" if minutes > 0 else "now"

    # Build header block
    header_lines = [
        f":calendar: *Meeting in {mins_text}* — _{title}_",
        f"*Time:* {time_str}",
    ]
    if external:
        header_lines.append(f"*With:* {', '.join(external[:3])}")
    if meeting.get("meet_link"):
        header_lines.append(f"*Link:* {meeting['meet_link']}")

    header_text = "\n".join(header_lines)

    # Run the Research Agent for a full pre-meeting brief
    if company_name:
        logger.info("A4: Running research agent for company '%s'", company_name)
        try:
            brief = await run_research_agent(
                company_name=company_name,
                meeting_title=title,
                attendees=external,
                minutes_until_meeting=minutes,
            )
        except Exception as exc:
            logger.warning("A4: Research agent failed for '%s': %s", company_name, exc)
            # Fall back to basic Attio context
            brief = await _basic_attio_context(company_name)
    else:
        brief = "_No company name detected — log the deal in Attio after the call._"

    full_text = f"{header_text}\n\n{brief}\n\n_Remember to update Attio after this call._"

    # DM the organizer
    organizer_email = meeting.get("organizer", "")
    try:
        if organizer_email:
            user_resp = await slack_client.users_lookupByEmail(email=organizer_email)
            user_id = user_resp["user"]["id"]
            await slack_client.chat_postMessage(channel=user_id, text=full_text)
            logger.info(
                "A4: Research nudge sent to %s for meeting '%s'", organizer_email, title
            )
        else:
            logger.warning("A4: No organizer email for meeting '%s'", title)
    except Exception as exc:
        logger.error("A4: Failed to DM nudge: %s", exc)


def _extract_company(title: str, external_emails: list[str]) -> str:
    """
    Best-effort extraction of company name from meeting title or attendee domain.
    Examples:
      "Nike Q2 scope call" → "Nike"
      attendee "sarah@adidas.com" → "adidas"
    """
    # Try first word(s) of title (heuristic: stop at common verbs/prepositions)
    stop_words = {"call", "meeting", "sync", "intro", "with", "q1", "q2", "q3", "q4"}
    words = title.split()
    company_words = []
    for w in words:
        if w.lower() in stop_words:
            break
        company_words.append(w)
    if company_words:
        return " ".join(company_words)

    # Fall back to first external attendee domain
    for email in external_emails:
        domain = email.split("@")[-1]
        parts = domain.split(".")
        if parts:
            return parts[0].capitalize()

    return ""


async def _basic_attio_context(company_name: str) -> str:
    """Fallback: simple Attio deal lookup without web research."""
    deal = await attio.find_deal_by_name(company_name)
    if deal:
        return (
            f"*Deal context:*\n{AttioClient.format_deal_line(deal, show_owner=True)}\n"
            f"Current probability: *{int(AttioClient._deal_probability(deal) or 0)}%* "
            f"· Stage: *{AttioClient._deal_stage(deal)}*"
        )
    return f"_No matching deal found in Attio for '{company_name}'._"
