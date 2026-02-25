"""
B1 — Monday Pipeline Forecast
Every Monday at 9 AM: posts a full pipeline digest to #deal-radar.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from collections import defaultdict

from slack_sdk.web.async_client import AsyncWebClient

import config
from clients.attio import attio, AttioClient
from agents.viktor import generate_monday_forecast_narrative

logger = logging.getLogger(__name__)

WON_STAGES = {"closed won", "won"}
LOST_STAGES = {"closed lost", "lost"}


async def post_monday_forecast(slack_client: AsyncWebClient) -> None:
    """Build and post the weekly pipeline forecast to #deal-radar."""
    try:
        all_deals = await attio.list_deals()
    except Exception as exc:
        logger.error("B1: Failed to fetch deals: %s", exc)
        return

    active = [
        d for d in all_deals
        if AttioClient._deal_stage(d).lower() not in WON_STAGES | LOST_STAGES
    ]

    # Filter by min probability
    forecast_deals = [
        d for d in active
        if (p := AttioClient._deal_probability(d)) is not None
        and p >= config.FORECAST_MIN_PROBABILITY
    ]
    forecast_deals.sort(
        key=lambda d: AttioClient._deal_probability(d) or 0, reverse=True
    )

    # Pipeline metrics
    total_value = sum(
        AttioClient._deal_value(d) or 0 for d in forecast_deals
    )
    weighted_value = sum(
        (AttioClient._deal_value(d) or 0) * (AttioClient._deal_probability(d) or 0) / 100
        for d in forecast_deals
    )

    # Group by stage
    by_stage: dict[str, list[dict]] = defaultdict(list)
    for d in forecast_deals:
        by_stage[AttioClient._deal_stage(d) or "Unknown"].append(d)

    now = datetime.now(timezone.utc)
    week_str = now.strftime("Week of %B %d, %Y")

    # Claude narrative
    narrative = await generate_monday_forecast_narrative(
        forecast_deals, total_value, weighted_value
    )

    # ── Build Slack blocks ────────────────────────────────────────────
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Pipeline Forecast — {week_str}"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": narrative},
        },
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f"*Total Pipeline*\n${total_value:,.0f}",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*Weighted Value*\n${weighted_value:,.0f}",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*Active Deals*\n{len(forecast_deals)}",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*Min Probability*\n{config.FORECAST_MIN_PROBABILITY}%+",
                },
            ],
        },
        {"type": "divider"},
    ]

    # Deal list by stage
    stage_order = [
        "Negotiation", "Proposal Sent", "Qualified", "Lead", "Unknown"
    ]
    sorted_stages = sorted(
        by_stage.keys(),
        key=lambda s: stage_order.index(s) if s in stage_order else 99,
    )

    for stage in sorted_stages:
        deals = by_stage[stage]
        lines = []
        for deal in deals[:10]:  # cap per stage
            name = AttioClient._deal_name(deal)
            prob = AttioClient._deal_probability(deal)
            value = AttioClient._deal_value(deal)
            close = AttioClient._deal_close_date(deal)

            prob_str = f"*{int(prob)}%*" if prob is not None else "–%"
            value_str = f"${value:,.0f}" if value else "–"
            close_str = close.strftime("%b %d") if close else "–"
            lines.append(f"• {prob_str} *{name}* · {value_str} · closes {close_str}")

        stage_text = f"*{stage}* ({len(deals)} deal{'s' if len(deals) != 1 else ''})\n"
        stage_text += "\n".join(lines)

        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": stage_text}}
        )

    blocks.append({"type": "divider"})
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "Ask Rabbit anything: `@Rabbit what deals close in March above 60%?`",
                }
            ],
        }
    )

    try:
        await slack_client.chat_postMessage(
            channel=config.DEAL_RADAR_CHANNEL_ID,
            blocks=blocks,
            text=f"Pipeline Forecast — {week_str}",
        )
        logger.info("B1: Monday forecast posted (%d deals).", len(forecast_deals))
    except Exception as exc:
        logger.error("B1: Failed to post forecast: %s", exc)
