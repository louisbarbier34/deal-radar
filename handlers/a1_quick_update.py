"""
A1 — Slack Quick Update
@Rabbit update <deal> to <X>% / move <deal> to <stage> / set <deal> close date to <date>

Called from the Slack app_mention event handler in main.py.
"""
from __future__ import annotations

import logging
from datetime import datetime

from clients.attio import attio, AttioClient
from agents.viktor import parse_intent

logger = logging.getLogger(__name__)


async def handle_quick_update(text: str, say, client) -> None:
    """
    Parse the user's message, find the deal in Attio, apply the update,
    and reply in Slack.
    """
    intent = await parse_intent(text)

    if intent.get("intent") != "update_deal":
        await say(
            "I didn't catch an update command. Try: "
            "`@Rabbit update Nike to 85%` or `@Rabbit move Nike to Proposal Sent`"
        )
        return

    deal_name: str = intent.get("deal_name") or ""
    field: str = intent.get("field") or ""
    new_value = intent.get("new_value")

    if not deal_name:
        await say("Which deal should I update? I didn't catch the name.")
        return
    if not field or new_value is None:
        await say(f"What should I update on *{deal_name}*?")
        return

    # Find deal in Attio
    deal = await attio.find_deal_by_name(deal_name)
    if not deal:
        await say(
            f"I couldn't find a deal matching *{deal_name}* in Attio. "
            "Check the spelling or try the exact name."
        )
        return

    record_id: str = deal.get("id", {}).get("record_id", "")
    actual_name = AttioClient._deal_name(deal)

    # Build the attribute patch
    attributes: dict = {}
    response_detail = ""

    if field == "probability":
        try:
            prob = float(str(new_value).replace("%", ""))
            attributes["probability"] = prob
            response_detail = f"probability → *{int(prob)}%*"
        except ValueError:
            await say(f"I couldn't parse `{new_value}` as a percentage.")
            return

    elif field == "stage":
        attributes["stage"] = str(new_value)
        response_detail = f"stage → *{new_value}*"

    elif field == "close_date":
        try:
            # Try a few common date formats
            for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%B %d %Y", "%b %d %Y", "%d %b %Y"):
                try:
                    dt = datetime.strptime(str(new_value), fmt)
                    attributes["close_date"] = dt.strftime("%Y-%m-%d")
                    response_detail = f"close date → *{dt.strftime('%b %d, %Y')}*"
                    break
                except ValueError:
                    continue
            if not attributes:
                await say(f"I couldn't parse `{new_value}` as a date. Try YYYY-MM-DD.")
                return
        except Exception:
            await say(f"I couldn't parse `{new_value}` as a date.")
            return

    elif field == "value":
        try:
            val = float(str(new_value).replace("$", "").replace(",", ""))
            attributes["value"] = val
            response_detail = f"value → *${val:,.0f}*"
        except ValueError:
            await say(f"I couldn't parse `{new_value}` as a dollar amount.")
            return

    elif field == "note":
        note_body = str(new_value)
        await attio.add_note(record_id, "Quick update via Rabbit", note_body)
        await say(f"Added a note to *{actual_name}* in Attio.")
        return

    else:
        await say(
            f"I don't know how to update `{field}`. "
            "I can update: probability, stage, close date, value, or add a note."
        )
        return

    # Apply patch
    try:
        await attio.update_deal(record_id, attributes)
        await say(f"Updated *{actual_name}*: {response_detail}")
        logger.info("A1: Updated deal %s (%s) — %s", actual_name, record_id, response_detail)
    except Exception as exc:
        logger.error("A1: Attio update failed: %s", exc)
        await say(f"Attio update failed: `{exc}`. Check the logs.")
