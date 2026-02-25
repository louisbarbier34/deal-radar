"""
B3 — Natural Language Query Handler
Handles @Viktor questions like:
  "What deals are above 70% closing in May?"
  "Show me everything in Negotiation"
  "What's our weighted pipeline for Q2?"
"""
from __future__ import annotations

import logging
from datetime import datetime

from clients.attio import attio, AttioClient
from agents.viktor import parse_intent, answer_pipeline_question

logger = logging.getLogger(__name__)


async def handle_nl_query(text: str, say) -> None:
    """
    Route a natural language question to the right data fetch + Viktor answer.
    """
    intent = await parse_intent(text)
    intent_type = intent.get("intent", "unknown")

    if intent_type == "update_deal":
        # Shouldn't land here — router in main.py handles this case first
        return

    # ── Fetch relevant deals ───────────────────────────────────────────
    try:
        if intent_type in ("query_pipeline", "forecast", "capacity_check"):
            deals = await _fetch_filtered_deals(intent)
        elif intent_type == "query_deal":
            deal_name = intent.get("deal_name", "")
            if deal_name:
                deal = await attio.find_deal_by_name(deal_name)
                deals = [deal] if deal else []
            else:
                deals = await attio.get_active_deals()
        else:
            deals = await attio.get_active_deals()
    except Exception as exc:
        logger.error("B3: Deal fetch failed: %s", exc)
        await say("I had trouble fetching deals from Attio. Try again in a moment.")
        return

    if not deals:
        await say("No deals match that query in Attio right now.")
        return

    # ── Ask Viktor to formulate the answer ────────────────────────────
    question = intent.get("question") or text
    try:
        answer = await answer_pipeline_question(question, deals)
        await say(answer)
    except Exception as exc:
        logger.error("B3: Claude query failed: %s", exc)
        # Graceful fallback: raw list
        lines = [AttioClient.format_deal_line(d) for d in deals[:15]]
        await say("Here's what I found:\n" + "\n".join(f"• {l}" for l in lines))


async def _fetch_filtered_deals(intent: dict) -> list[dict]:
    """Apply filters from the parsed intent to narrow the deal list."""
    filters = intent.get("filters") or {}
    min_prob: float | None = filters.get("min_probability")
    max_prob: float | None = filters.get("max_probability")
    month: int | None = filters.get("month")
    year: int | None = filters.get("year") or datetime.now().year
    stage_filter: str | None = filters.get("stage")

    all_active = await attio.get_active_deals()
    result = []

    for deal in all_active:
        prob = AttioClient._deal_probability(deal)
        stage = AttioClient._deal_stage(deal).lower()
        close = AttioClient._deal_close_date(deal)

        if min_prob is not None and (prob is None or prob < min_prob):
            continue
        if max_prob is not None and (prob is None or prob > max_prob):
            continue
        if month is not None:
            if close is None or close.month != month:
                continue
            if year is not None and close.year != year:
                continue
        if stage_filter and stage_filter.lower() not in stage:
            continue

        result.append(deal)

    return result
