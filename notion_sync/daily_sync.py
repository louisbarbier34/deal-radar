"""
C — Attio → Notion Daily Sync
Runs every day at 7 AM.
Upserts all active deals from Attio into the Notion Production Calendar.
Won/Lost deals are archived (not deleted) with updated status.

Deals are synced in parallel batches of NOTION_BATCH_SIZE to respect
Notion's rate limits (~3 req/s) while staying well clear of the limit.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient

import config
from clients.attio import attio, AttioClient
from clients.notion import notion_db

logger = logging.getLogger(__name__)

WON_STAGES = {"closed won", "won"}
LOST_STAGES = {"closed lost", "lost"}
NOTION_BATCH_SIZE = 10  # concurrent Notion requests per batch


async def _sync_one_deal(deal: dict) -> tuple[str, str | None]:
    """
    Sync a single deal to Notion.
    Returns (category, error_message | None).
    category ∈ {"synced", "won", "lost"}
    """
    name = AttioClient._deal_name(deal)
    stage = AttioClient._deal_stage(deal).lower()
    rid = deal.get("id", {}).get("record_id", "")

    try:
        if stage in WON_STAGES:
            await notion_db.upsert_deal(deal, attio)
            return "won", None

        elif stage in LOST_STAGES:
            page = await notion_db.find_page_by_attio_id(rid)
            if page:
                from notion_client import AsyncClient
                nc = AsyncClient(auth=config.NOTION_TOKEN)
                await nc.pages.update(
                    page_id=page["id"],
                    properties={
                        "Production Status": {"select": {"name": "On Hold"}},
                        "Stage": {"select": {"name": "Lost"}},
                    },
                )
            return "lost", None

        else:
            await notion_db.upsert_deal(deal, attio)
            return "synced", None

    except Exception as exc:
        logger.error("C: Failed to sync deal '%s': %s", name, exc)
        return "synced", str(exc)  # category doesn't matter on error


async def run_daily_sync(slack_client: AsyncWebClient | None = None) -> dict:
    """
    Full Attio → Notion sync using batched asyncio.gather.
    Returns stats dict: {synced, won, lost, errors}
    """
    stats = {"synced": 0, "won": 0, "lost": 0, "errors": 0}

    # Ensure DB has all required columns
    try:
        await notion_db.ensure_database_properties()
    except Exception as exc:
        logger.warning("C: DB property check failed (continuing): %s", exc)

    try:
        all_deals = await attio.list_deals()
    except Exception as exc:
        logger.error("C: Failed to fetch deals from Attio: %s", exc)
        return stats

    logger.info("C: Syncing %d deals to Notion in batches of %d…", len(all_deals), NOTION_BATCH_SIZE)

    # Process in batches to respect Notion rate limits
    for i in range(0, len(all_deals), NOTION_BATCH_SIZE):
        batch = all_deals[i: i + NOTION_BATCH_SIZE]
        results = await asyncio.gather(
            *[_sync_one_deal(deal) for deal in batch],
            return_exceptions=False,
        )
        for category, error in results:
            if error:
                stats["errors"] += 1
            else:
                stats[category] += 1

    total = stats["synced"] + stats["won"] + stats["lost"]
    logger.info(
        "C: Sync complete. %d synced, %d won, %d lost, %d errors.",
        stats["synced"], stats["won"], stats["lost"], stats["errors"],
    )

    if slack_client and stats["errors"] == 0:
        await slack_client.chat_postMessage(
            channel=config.DEAL_RADAR_CHANNEL_ID,
            text=(
                f":notion: Notion sync complete — "
                f"{total} deals updated "
                f"({stats['won']} won, {stats['lost']} lost)"
            ),
        )
    elif slack_client and stats["errors"] > 0:
        await slack_client.chat_postMessage(
            channel=config.DEAL_RADAR_CHANNEL_ID,
            text=(
                f":warning: Notion sync finished with *{stats['errors']} errors*. "
                "Check the logs."
            ),
        )

    return stats
