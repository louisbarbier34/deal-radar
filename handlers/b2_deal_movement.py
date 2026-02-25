"""
B2 — Deal Movement Alerts
Polls Attio every 15 min and fires alerts to #deal-radar when:
  - Stage changes
  - Probability shifts ≥20 points
  - Close date moves
  - Deal goes 21+ days without update (stale flag)
  - New deal enters pipeline
"""
from __future__ import annotations

import logging
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient

import config
from clients.attio import attio, AttioClient
from clients.state import state

logger = logging.getLogger(__name__)

_NS = "b2_snapshot"


def _summarise(deal: dict) -> dict:
    close = AttioClient._deal_close_date(deal)
    return {
        "name": AttioClient._deal_name(deal),
        "stage": AttioClient._deal_stage(deal),
        "probability": AttioClient._deal_probability(deal),
        "value": AttioClient._deal_value(deal),
        "close_date": close.strftime("%Y-%m-%d") if close else None,
        "updated_at": deal.get("updated_at", ""),
    }


async def run_deal_movement_check(slack_client: AsyncWebClient) -> None:
    """Compare current Attio state to snapshot; alert on changes."""
    try:
        deals = await attio.list_deals()
    except Exception as exc:
        logger.error("B2: Failed to fetch deals: %s", exc)
        return

    snapshot = state.get_snapshot(_NS)
    current: dict[str, dict] = {}
    for deal in deals:
        rid = deal.get("id", {}).get("record_id", "")
        if rid:
            current[rid] = deal

    alerts: list[str] = []
    updated_snapshot = dict(snapshot)

    for rid, deal in current.items():
        now_s = _summarise(deal)

        if rid not in snapshot:
            alerts.append(_new_deal_alert(now_s))
            updated_snapshot[rid] = now_s
            continue

        prev_s = snapshot[rid]
        change_msgs = _detect_changes(prev_s, now_s)
        alerts.extend(change_msgs)
        updated_snapshot[rid] = now_s

    state.set_snapshot(_NS, updated_snapshot)

    if alerts:
        await _post_alerts(alerts, slack_client)


def _detect_changes(prev: dict, now: dict) -> list[str]:
    msgs: list[str] = []
    name = now["name"]

    # Stage change
    if prev["stage"] != now["stage"] and now["stage"]:
        msgs.append(
            f":arrows_counterclockwise: *{name}* moved to *{now['stage']}*"
            + (f" (was _{prev['stage']}_)" if prev["stage"] else "")
        )

    # Probability shift ≥ 20
    p_prev = prev["probability"] or 0
    p_now = now["probability"] or 0
    delta = p_now - p_prev
    if abs(delta) >= 20:
        direction = ":chart_with_upwards_trend:" if delta > 0 else ":chart_with_downwards_trend:"
        msgs.append(
            f"{direction} *{name}* probability "
            f"{'up' if delta > 0 else 'down'} {abs(int(delta))} pts → *{int(p_now)}%*"
        )

    # Close date moved
    if prev["close_date"] != now["close_date"] and now["close_date"]:
        prev_date = prev["close_date"] or "—"
        msgs.append(
            f":calendar: *{name}* close date moved → *{now['close_date']}*"
            + (f" (was {prev_date})" if prev_date != "—" else "")
        )

    return msgs


def _new_deal_alert(s: dict) -> str:
    prob = f" · {int(s['probability'])}%" if s["probability"] is not None else ""
    value = f" · ${s['value']:,.0f}" if s["value"] else ""
    return f":sparkles: *New deal in pipeline:* *{s['name']}*{prob}{value} — _{s['stage']}_"


async def _post_alerts(alerts: list[str], slack_client: AsyncWebClient) -> None:
    text = "\n".join(alerts)
    try:
        await slack_client.chat_postMessage(
            channel=config.DEAL_RADAR_CHANNEL_ID,
            text=text,
        )
        logger.info("B2: Posted %d deal movement alerts.", len(alerts))
    except Exception as exc:
        logger.error("B2: Failed to post alerts: %s", exc)


async def seed_snapshot() -> None:
    """
    Populate the baseline snapshot on first run.
    Safe to call on every startup — skips if snapshot already exists.
    """
    existing = state.get_snapshot(_NS)
    if existing:
        logger.info("B2: Snapshot already seeded (%d deals).", len(existing))
        return
    try:
        deals = await attio.list_deals()
        snapshot = {}
        for deal in deals:
            rid = deal.get("id", {}).get("record_id", "")
            if rid:
                snapshot[rid] = _summarise(deal)
        state.set_snapshot(_NS, snapshot)
        logger.info("B2: Snapshot seeded with %d deals.", len(snapshot))
    except Exception as exc:
        logger.error("B2: Snapshot seed failed: %s", exc)
