"""
A5 — Weekly Hygiene Nudges
Every Monday at 9 AM, DM each deal owner about their stale deals
(no update in STALE_DEAL_DAYS days).
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone

from slack_sdk.web.async_client import AsyncWebClient

import config
from clients.attio import attio, AttioClient

logger = logging.getLogger(__name__)


async def run_hygiene_nudges(slack_client: AsyncWebClient) -> None:
    """Group stale deals by owner and DM each owner."""
    try:
        stale = await attio.get_stale_deals()
    except Exception as exc:
        logger.error("A5: Failed to fetch stale deals: %s", exc)
        return

    if not stale:
        logger.info("A5: No stale deals found.")
        return

    # Fetch Slack users ONCE — shared across all DMs
    try:
        users_resp = await slack_client.users_list()
        slack_users = users_resp.get("members", [])
    except Exception as exc:
        logger.error("A5: Failed to fetch Slack users: %s", exc)
        slack_users = []

    # Group by owner
    by_owner: dict[str, list[dict]] = defaultdict(list)
    for deal in stale:
        owner = AttioClient._deal_owner(deal) or "unknown"
        by_owner[owner].append(deal)

    logger.info("A5: Sending hygiene nudges to %d owners.", len(by_owner))

    for owner_name, deals in by_owner.items():
        await _dm_owner(owner_name, deals, slack_client, slack_users)


async def _dm_owner(
    owner_name: str,
    stale_deals: list[dict],
    slack_client: AsyncWebClient,
    slack_users: list[dict],
) -> None:
    deal_lines = []
    for deal in stale_deals:
        name = AttioClient._deal_name(deal)
        stage = AttioClient._deal_stage(deal)
        prob = AttioClient._deal_probability(deal)
        last = AttioClient._deal_last_updated(deal)

        days_stale = ""
        if last:
            delta = datetime.now(timezone.utc) - last
            days_stale = f" — _last updated {delta.days}d ago_"

        prob_str = f" ({int(prob)}%)" if prob is not None else ""
        deal_lines.append(f"• *{name}*{prob_str} · {stage}{days_stale}")

    count = len(stale_deals)
    message = (
        f":wave: *Weekly pipeline hygiene check*\n\n"
        f"You have *{count} stale deal{'s' if count != 1 else ''}* "
        f"with no update in {config.STALE_DEAL_DAYS}+ days:\n\n"
        + "\n".join(deal_lines)
        + "\n\nPlease update probability, stage, or add a note in Attio. "
        "Reply `@Rabbit update <deal> to <X>%` to update from here."
    )

    # Match owner name to Slack user from the pre-fetched list
    matched_user = next(
        (
            u
            for u in slack_users
            if not u.get("deleted")
            and owner_name.lower()
            in (u.get("real_name", "") + u.get("name", "")).lower()
        ),
        None,
    )

    try:
        if matched_user:
            await slack_client.chat_postMessage(
                channel=matched_user["id"], text=message
            )
            logger.info("A5: Hygiene nudge sent to %s (%d deals)", owner_name, count)
        else:
            # Fall back to #deal-radar mention
            await slack_client.chat_postMessage(
                channel=config.DEAL_RADAR_CHANNEL_ID,
                text=f"_Hygiene nudge for {owner_name} (couldn't find Slack user):_\n{message}",
            )
    except Exception as exc:
        logger.error("A5: Failed to send nudge to %s: %s", owner_name, exc)
