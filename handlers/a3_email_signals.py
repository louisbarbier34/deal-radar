"""
A3 — Email Signal Detection
Runs on a schedule (every 4 hours) to scan Gmail for deal-relevant emails.
Posts signals to #deal-radar when it finds something actionable.

Now backed by the Signal Agent for richer extraction:
  - Searches Attio for the best deal match
  - Returns structured confidence + candidates
  - Auto-logs when confidence is high (no human needed)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from slack_sdk.web.async_client import AsyncWebClient

import config
from clients.attio import attio, AttioClient
from clients.gmail import gmail
from clients.state import state
from agents.signal_agent import run_signal_agent

logger = logging.getLogger(__name__)

_NS = "a3_email"


async def run_email_scan(slack_client: AsyncWebClient) -> None:
    """
    Scan Gmail for deal signals in the last 6 hours.
    Post any new findings to #deal-radar.
    """
    since = datetime.now(timezone.utc) - timedelta(hours=6)

    try:
        signals = gmail.scan_for_deal_signals(after_timestamp=since, max_results=30)
    except Exception as exc:
        logger.error("A3: Gmail scan failed: %s", exc)
        return

    new_signals = [s for s in signals if not state.has_processed(_NS, s["message_id"])]
    if not new_signals:
        logger.debug("A3: No new email signals.")
        return

    logger.info("A3: Found %d new email signals.", len(new_signals))

    for signal in new_signals:
        state.mark_processed(_NS, signal["message_id"])
        await _post_email_signal(signal, slack_client)


async def _post_email_signal(signal: dict, slack_client: AsyncWebClient) -> None:
    """Post a single email signal to #deal-radar, powered by the Signal Agent."""
    extracted = await run_signal_agent(
        signal["body_preview"],
        context=f"Email from {signal['sender']} — Subject: {signal['subject']}",
        source="email",
        auto_log=False,  # Always surface in Slack for review
    )

    deal_name = extracted.get("deal_name")
    suggested_note = extracted.get("note_body") or signal["snippet"]
    key_signals = extracted.get("key_signals") or signal["matched_keywords"]
    urgency = extracted.get("urgency", "medium")
    confidence = extracted.get("confidence", "low")

    urgency_emoji = {
        "high": ":red_circle:",
        "medium": ":large_yellow_circle:",
        "low": ":white_circle:",
    }.get(urgency, ":white_circle:")

    # Try to find existing deal for cross-link
    deal_link_text = ""
    if deal_name:
        deal = await attio.find_deal_by_name(deal_name)
        if deal:
            deal_link_text = f"\n*Matched deal:* {AttioClient.format_deal_line(deal)}"

    date_str = signal["date"].strftime("%b %d, %I:%M %p") if signal["date"] else "Unknown date"

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{urgency_emoji} *Email Signal Detected*\n\n"
                    f"*From:* {signal['sender']}\n"
                    f"*Subject:* {signal['subject']}\n"
                    f"*Date:* {date_str}\n"
                    f"*Keywords:* {', '.join(f'`{k}`' for k in key_signals[:6])}"
                    f"{deal_link_text}"
                ),
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Summary:*\n_{suggested_note}_",
            },
        },
    ]

    # Show candidates if ambiguous
    candidates = extracted.get("candidates", [])
    if candidates and confidence == "medium":
        cand_lines = "\n".join(
            f"• *{c.get('name', '?')}* — {c.get('stage', '?')}"
            for c in candidates[:3]
        )
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Possible deal matches:*\n{cand_lines}",
                },
            }
        )

    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Log to Attio"},
                    "style": "primary",
                    "action_id": "log_email_signal_to_attio",
                    "value": f"{deal_name or 'unknown'}|||{suggested_note[:500]}",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Ignore"},
                    "action_id": "dismiss_email_signal",
                    "value": signal["message_id"],
                },
            ],
        }
    )

    try:
        await slack_client.chat_postMessage(
            channel=config.DEAL_RADAR_CHANNEL_ID,
            blocks=blocks,
            text=f"Email signal: {signal['subject']}",
        )
    except Exception as exc:
        logger.error("A3: Failed to post signal to Slack: %s", exc)
