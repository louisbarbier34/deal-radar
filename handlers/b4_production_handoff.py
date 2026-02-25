"""
B4 — Production Handoff Brief
Triggered when a deal reaches Won stage (detected by B2 movement check).
Posts a full production brief to #deal-radar and creates the Notion plan.

Now backed by the Production Planner Agent, which:
  - Infers deliverable type and duration from deal data
  - Generates week-by-week schedule
  - Checks for capacity conflicts
  - Writes the full plan to Notion
  - Returns a Slack-formatted brief
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from slack_sdk.web.async_client import AsyncWebClient

import config
from clients.attio import attio, AttioClient
from clients.state import state
from agents.production_planner_agent import run_production_planner

logger = logging.getLogger(__name__)

WON_STAGES = {"closed won", "won"}
_NS = "b4_handoffs"


async def check_and_post_handoffs(slack_client: AsyncWebClient) -> None:
    """
    Called by the scheduler every 15 min.
    Scans for recently-won deals and posts handoff briefs.
    """
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    try:
        won_deals = await attio.get_won_deals_since(since)
    except Exception as exc:
        logger.error("B4: Failed to fetch won deals: %s", exc)
        return

    for deal in won_deals:
        rid = deal.get("id", {}).get("record_id", "")
        if state.has_processed(_NS, rid):
            continue
        await post_handoff_brief(deal, slack_client)
        state.mark_processed(_NS, rid)


async def post_handoff_brief(deal: dict, slack_client: AsyncWebClient) -> None:
    """Generate and post a production handoff brief for a won deal."""
    name = AttioClient._deal_name(deal)
    value = AttioClient._deal_value(deal)
    owner = AttioClient._deal_owner(deal)
    close = AttioClient._deal_close_date(deal)

    logger.info("B4: Running production planner for deal: %s", name)

    try:
        # Production Planner Agent handles Notion creation + returns Slack brief
        brief = await run_production_planner(deal)
    except Exception as exc:
        logger.error("B4: Production planner failed: %s", exc)
        brief = (
            f"*{name}* has been marked Won. "
            "Please create the production plan in Notion manually."
        )

    value_str = f"${value:,.0f}" if value else "—"
    close_str = close.strftime("%B %d, %Y") if close else "—"

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"🎉 Deal Won: {name}"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Value:*\n{value_str}"},
                {"type": "mrkdwn", "text": f"*Close Date:*\n{close_str}"},
                {"type": "mrkdwn", "text": f"*Deal Owner:*\n{owner or '—'}"},
                {"type": "mrkdwn", "text": f"*Stage:*\nWon :white_check_mark:"},
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": brief},
        },
        {"type": "divider"},
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        "Production plan created in Notion by Rabbit. :rabbit2: "
                        "Assign a production lead and confirm crew availability."
                    ),
                }
            ],
        },
    ]

    try:
        await slack_client.chat_postMessage(
            channel=config.DEAL_RADAR_CHANNEL_ID,
            blocks=blocks,
            text=f"Deal Won: {name} — {value_str}",
        )
        logger.info("B4: Handoff brief posted for %s", name)
    except Exception as exc:
        logger.error("B4: Failed to post brief: %s", exc)
