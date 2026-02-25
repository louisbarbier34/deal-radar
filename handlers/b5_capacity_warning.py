"""
B5 — Capacity Conflict Warnings
Runs daily. Flags months where multiple high-probability deals
target the same production window, potentially overloading crew.
High-prob threshold: ≥60% probability.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone

from slack_sdk.web.async_client import AsyncWebClient

import config
from clients.attio import attio, AttioClient

logger = logging.getLogger(__name__)

HIGH_PROB_THRESHOLD = 60  # % — deals above this are "likely" productions
CONFLICT_THRESHOLD = 2    # How many high-prob deals in one month = conflict


async def run_capacity_check(slack_client: AsyncWebClient) -> None:
    """Identify months with potential crew capacity conflicts."""
    try:
        active = await attio.get_active_deals()
    except Exception as exc:
        logger.error("B5: Failed to fetch deals: %s", exc)
        return

    now = datetime.now(timezone.utc)
    cutoff_month = (now.year, now.month)

    # Group high-prob deals by (year, month) of close date
    by_month: dict[tuple, list[dict]] = defaultdict(list)
    for deal in active:
        prob = AttioClient._deal_probability(deal)
        close = AttioClient._deal_close_date(deal)
        if prob is None or prob < HIGH_PROB_THRESHOLD or close is None:
            continue
        month_key = (close.year, close.month)
        if month_key < cutoff_month:
            continue  # past months, skip
        by_month[month_key].append(deal)

    conflicts = {k: v for k, v in by_month.items() if len(v) >= CONFLICT_THRESHOLD}
    if not conflicts:
        logger.info("B5: No capacity conflicts detected.")
        return

    await _post_capacity_warning(conflicts, slack_client)


async def _post_capacity_warning(
    conflicts: dict[tuple, list[dict]],
    slack_client: AsyncWebClient,
) -> None:
    sections = []

    for (year, month), deals in sorted(conflicts.items()):
        month_name = datetime(year, month, 1).strftime("%B %Y")
        total_value = sum(AttioClient._deal_value(d) or 0 for d in deals)
        weighted = sum(
            (AttioClient._deal_value(d) or 0) * (AttioClient._deal_probability(d) or 0) / 100
            for d in deals
        )

        deal_lines = []
        for d in deals:
            name = AttioClient._deal_name(d)
            prob = AttioClient._deal_probability(d)
            val = AttioClient._deal_value(d)
            prob_str = f"{int(prob)}%" if prob is not None else "–"
            val_str = f"${val:,.0f}" if val else "–"
            deal_lines.append(f"  • *{name}* — {prob_str} · {val_str}")

        text = (
            f":warning: *{month_name}* — {len(deals)} high-prob deals targeting this month\n"
            + "\n".join(deal_lines)
            + f"\n  _Pipeline: ${total_value:,.0f} total · ${weighted:,.0f} weighted_"
        )
        sections.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
        sections.append({"type": "divider"})

    header_text = (
        f":construction: *Capacity Conflict Warning*\n"
        f"{len(conflicts)} month{'s' if len(conflicts) != 1 else ''} with "
        f"{CONFLICT_THRESHOLD}+ high-probability deals (≥{HIGH_PROB_THRESHOLD}%) "
        "targeting the same production window."
    )

    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": header_text},
        },
        {"type": "divider"},
        *sections,
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "Review crew availability and production scheduling. "
                    "Consider staggering close dates or pre-booking crew.",
                }
            ],
        },
    ]

    try:
        await slack_client.chat_postMessage(
            channel=config.DEAL_RADAR_CHANNEL_ID,
            blocks=blocks,
            text=f"Capacity conflict: {len(conflicts)} months flagged",
        )
        logger.info("B5: Capacity warning posted for %d months.", len(conflicts))
    except Exception as exc:
        logger.error("B5: Failed to post capacity warning: %s", exc)
