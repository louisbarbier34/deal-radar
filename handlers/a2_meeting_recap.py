"""
A2 — Meeting Recap → Attio
Watches for messages posted in Slack with meeting-recap-like content
(triggered from message_posted event or explicit @Rabbit recap command).

Also used as a standalone function that can be called after a Google Meet
ends (via Pipedream webhook).

Now backed by the Signal Agent for richer, multi-step extraction:
  - Searches Attio to confirm the deal match
  - Handles ambiguity (multiple candidates)
  - Auto-logs when confidence is high (Pipedream path)
"""
from __future__ import annotations

import logging
import re

from clients.attio import attio, AttioClient
from agents.signal_agent import run_signal_agent

logger = logging.getLogger(__name__)

# Patterns that suggest a message is a meeting recap
RECAP_PATTERNS = re.compile(
    r"meeting notes?|call notes?|recap|debrief|follow.?up|discussed|agreed|next steps?|action items?",
    re.IGNORECASE,
)


async def handle_recap_message(text: str, say, channel: str, user: str) -> None:
    """
    Called when a team member posts what looks like a meeting recap.
    Runs the Signal Agent to extract deal signals, then offers to log them.
    """
    if not RECAP_PATTERNS.search(text):
        return  # Not a recap, ignore

    result = await run_signal_agent(
        text,
        context="Slack meeting recap",
        source="slack_recap",
        auto_log=False,  # Always prompt for confirmation in Slack
    )

    deal_name = result.get("deal_name")
    key_signals = result.get("key_signals", [])
    suggested_note = result.get("note_body", "")
    action_items = result.get("action_items", [])
    confidence = result.get("confidence", "low")

    if not deal_name and not key_signals:
        return  # No meaningful signals found

    confidence_emoji = {"high": ":large_green_circle:", "medium": ":large_yellow_circle:", "low": ":white_circle:"}.get(
        confidence, ":white_circle:"
    )

    # Build Slack response
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":rabbit2: *Rabbit spotted a deal signal in your recap* {confidence_emoji}\n\n"
                    f"*Detected deal:* {deal_name or '_unknown_'}\n"
                    f"*Signals:* {', '.join(key_signals) if key_signals else 'See note below'}\n"
                    f"*Confidence:* {confidence}"
                ),
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Suggested Attio note:*\n_{suggested_note or 'No note generated.'}_",
            },
        },
    ]

    if action_items:
        actions_text = "\n".join(f"• {a}" for a in action_items[:5])
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Action items:*\n{actions_text}"},
            }
        )

    # If multiple candidates (medium confidence), list them
    candidates = result.get("candidates", [])
    if candidates and confidence == "medium":
        cand_lines = "\n".join(
            f"• *{c.get('name', '?')}* — {c.get('stage', '?')} ({c.get('probability', '?')}%)"
            for c in candidates[:4]
        )
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Multiple deal matches found — which one?*\n{cand_lines}",
                },
            }
        )

    # Action buttons
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Log to Attio"},
                    "style": "primary",
                    "action_id": "log_recap_to_attio",
                    "value": f"{deal_name}|||{suggested_note}",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Dismiss"},
                    "action_id": "dismiss_recap",
                },
            ],
        }
    )

    await say(blocks=blocks)


async def log_recap_to_attio(deal_name: str, note_text: str) -> str:
    """
    Find the deal in Attio and add the note.
    Returns a confirmation string for the Slack response.
    """
    deal = await attio.find_deal_by_name(deal_name)
    if not deal:
        return (
            f"Couldn't find *{deal_name}* in Attio. "
            "Log it manually or check the deal name."
        )

    record_id = deal.get("id", {}).get("record_id", "")
    actual_name = AttioClient._deal_name(deal)
    await attio.add_note(record_id, "Meeting recap (via Rabbit)", note_text)
    logger.info("A2: Logged recap note to deal %s (%s)", actual_name, record_id)
    return f"Note logged to *{actual_name}* in Attio. :white_check_mark:"


async def process_pipedream_webhook(payload: dict) -> dict:
    """
    Entry point for Pipedream workflow webhooks.
    Payload: {source: "google_meet"|"zoom", transcript: str, attendees: list}
    Returns: {deal_name, note_logged, attio_record_id}
    """
    transcript = payload.get("transcript", "")
    attendees = payload.get("attendees", [])
    source = payload.get("source", "meeting")

    if not transcript:
        return {"error": "No transcript provided"}

    context = f"Auto-transcription from {source}. Attendees: {', '.join(attendees)}"

    # Auto-log when confidence is high (unattended Pipedream path)
    result = await run_signal_agent(
        transcript,
        context=context,
        source="meeting_transcript",
        auto_log=True,
    )

    return {
        "deal_name": result.get("deal_name"),
        "note_logged": result.get("logged", False),
        "attio_record_id": result.get("record_id"),
        "confidence": result.get("confidence"),
        "signals": result.get("key_signals", []),
    }
