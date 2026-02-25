"""
Production Planner Agent — Won deal → week-by-week production plan + Notion.

Given a won deal dict (from Attio), this agent:
  1. Analyses the deal name + value to infer deliverable type and scope
  2. Reasons about a realistic week-by-week production schedule
     (pre-production → shoot/production → post → delivery)
  3. Checks for capacity conflicts with other active deals in that window
  4. Writes the full plan to the Notion Production Calendar
  5. Returns a Slack-formatted production brief for #deal-radar

Usage:
    from agents.production_planner_agent import run_production_planner
    brief = await run_production_planner(deal)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import anthropic

import config
from clients.attio import attio, AttioClient
from clients.notion import notion_db

logger = logging.getLogger(__name__)

MAX_TURNS = 6
MODEL = "claude-sonnet-4-6"  # Needs strong reasoning for production scheduling

_claude = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

# ─── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are Rabbit, production scheduling agent for Wonder Studios — a VFX and production company.

A deal has just been won. Your job: build a week-by-week production plan and write it to the Notion Production Calendar.

## Deliverable Duration Guidelines
- Commercial / TVC:     4-8 weeks  (pre-prod 1-2w · shoot 1w · post/VFX 2-4w · delivery 1w)
- Brand Content/Social: 3-5 weeks  (pre-prod 1w · shoot 1w · post 1-2w · delivery 1w)
- Film (VFX work):     12-24 weeks (pre-prod 2-4w · production ongoing · VFX 6-16w · QC+delivery 2w)
- TV Series:            8-14 weeks per episode block (pre-prod 2w · production 3-4w · post 3-6w · delivery 1w)
- Other: scale with value — <$30k = social (3-4w), $30-80k = TVC (5-7w), >$80k = film/series scope

## Your Process (always follow in order)
1. Infer deliverable type from deal name + value using the guidelines above
2. Set projected_start = today + 7-14 days (NEVER use today as the start date)
3. Build a week-by-week phase breakdown: Pre-Production → Shoot/Production → Post/VFX → QC → Delivery
4. ALWAYS call get_pipeline_for_capacity_check with (start_date=projected_start, end_date=delivery_date) — never skip this
5. If conflicts found, prepend a ⚠️ Capacity Warning to crew_notes listing conflicting deals
6. Call write_production_plan_to_notion with the full plan
7. Post a concise Slack brief (< 250 words)

## Rules
- All dates must be ISO YYYY-MM-DD — no exceptions
- projected_start must be at minimum 7 days after today
- crew_notes must contain the full week-by-week schedule AND crew requirements (Director, DP, VFX Supervisor, etc.)
- If deal name is ambiguous about deliverable type, use value as the tiebreaker
- After write_production_plan_to_notion succeeds, confirm the Notion action in your brief
- Today: {today}"""

# ─── Tool definitions ──────────────────────────────────────────────────────────

TOOLS: list[dict] = [
    {
        "name": "get_pipeline_for_capacity_check",
        "description": (
            "Retrieve active deals whose close dates fall within a production window. "
            "Use this to flag potential crew capacity conflicts before writing the plan."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {
                    "type": "string",
                    "description": "Planned production start date (ISO YYYY-MM-DD).",
                },
                "end_date": {
                    "type": "string",
                    "description": "Planned production end date / delivery date (ISO YYYY-MM-DD).",
                },
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "write_production_plan_to_notion",
        "description": (
            "Create or update the Notion Production Calendar entry with a full production plan. "
            "Call this once you have determined the deliverable type, schedule, and crew notes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "attio_record_id": {
                    "type": "string",
                    "description": "Attio record ID of the deal.",
                },
                "project_name": {
                    "type": "string",
                    "description": "Full project name from the deal.",
                },
                "deliverable_type": {
                    "type": "string",
                    "enum": ["Commercial", "Film", "TV Series", "Brand Content", "Other"],
                    "description": "Inferred deliverable category.",
                },
                "projected_start": {
                    "type": "string",
                    "description": "Planned kick-off date (ISO YYYY-MM-DD). Usually 1-2 weeks from today.",
                },
                "duration_weeks": {
                    "type": "integer",
                    "description": "Total production duration in weeks.",
                },
                "production_lead": {
                    "type": "string",
                    "description": "Deal owner name (will be suggested as production lead).",
                },
                "deal_value": {
                    "type": "number",
                    "description": "Deal value in dollars (for the Notion record).",
                },
                "close_date": {
                    "type": "string",
                    "description": "Contract close/delivery date (ISO YYYY-MM-DD).",
                },
                "crew_notes": {
                    "type": "string",
                    "description": (
                        "Week-by-week production schedule and crew requirements. "
                        "Format: 'Week 1-2: Pre-production (script breakdown, location scout)\\n"
                        "Week 3: Principal photography\\n"
                        "Week 4-6: VFX & edit\\nWeek 7: Client review & delivery\\n"
                        "Crew: Director, DP, VFX Supervisor, 2x Compositors'"
                    ),
                },
            },
            "required": [
                "attio_record_id",
                "project_name",
                "deliverable_type",
                "projected_start",
                "duration_weeks",
                "crew_notes",
            ],
        },
    },
]

# ─── Tool implementations ───────────────────────────────────────────────────────

async def _tool_capacity_check(start_date: str, end_date: str) -> list[dict]:
    """Find active deals whose close dates overlap with the given production window."""
    try:
        start = datetime.fromisoformat(start_date)
        end = datetime.fromisoformat(end_date)
    except ValueError:
        return [{"error": f"Invalid date format: {start_date} / {end_date}"}]

    active = await attio.get_active_deals()
    conflicts = []
    for d in active:
        close = AttioClient._deal_close_date(d)
        if close and start <= close <= end:
            conflicts.append({
                "name": AttioClient._deal_name(d),
                "stage": AttioClient._deal_stage(d),
                "probability": AttioClient._deal_probability(d),
                "value": AttioClient._deal_value(d),
                "close_date": close.strftime("%Y-%m-%d"),
                "owner": AttioClient._deal_owner(d),
            })
    return conflicts


async def _tool_write_notion_plan(
    attio_record_id: str,
    project_name: str,
    deliverable_type: str,
    projected_start: str,
    duration_weeks: int,
    crew_notes: str,
    production_lead: str = "",
    deal_value: float | None = None,
    close_date: str | None = None,
) -> dict:
    """Write (upsert) the production plan to Notion."""
    try:
        start_dt = datetime.fromisoformat(projected_start).replace(tzinfo=timezone.utc)
    except ValueError:
        start_dt = datetime.now(timezone.utc) + timedelta(weeks=1)

    close_dt: datetime | None = None
    if close_date:
        try:
            close_dt = datetime.fromisoformat(close_date).replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    props = notion_db._build_properties(
        project_name=project_name,
        deliverable_type=deliverable_type,
        projected_start=start_dt,
        duration_weeks=duration_weeks,
        production_lead=production_lead,
        crew_notes=crew_notes[:2000],
        production_status="Pre-Production",
        attio_record_id=attio_record_id,
        deal_value=deal_value,
        close_date=close_dt,
    )

    # Try to find existing page first
    existing = await notion_db.find_page_by_attio_id(attio_record_id)
    if existing:
        await notion_db._client.pages.update(page_id=existing["id"], properties=props)
        action = "updated"
    else:
        await notion_db._client.pages.create(
            parent={"database_id": notion_db._db_id}, properties=props
        )
        action = "created"

    return {
        "success": True,
        "action": action,
        "project": project_name,
        "deliverable_type": deliverable_type,
        "duration_weeks": duration_weeks,
        "projected_start": projected_start,
    }


async def _execute_tool(name: str, inputs: dict) -> Any:
    try:
        if name == "get_pipeline_for_capacity_check":
            return await _tool_capacity_check(**inputs)
        elif name == "write_production_plan_to_notion":
            return await _tool_write_notion_plan(**inputs)
        else:
            return {"error": f"Unknown tool: {name}"}
    except Exception as exc:
        logger.error("Production planner tool %s failed: %s", name, exc)
        return {"error": str(exc)}


# ─── Main agent ─────────────────────────────────────────────────────────────────

async def run_production_planner(deal: dict) -> str:
    """
    Run the production planner agent for a won deal.

    Args:
        deal: Raw Attio deal dict (as returned by list_deals / get_won_deals_since).

    Returns:
        Slack-formatted production brief string.
    """
    name = AttioClient._deal_name(deal)
    value = AttioClient._deal_value(deal)
    owner = AttioClient._deal_owner(deal)
    close = AttioClient._deal_close_date(deal)
    record_id = deal.get("id", {}).get("record_id", "")

    today = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
    system = SYSTEM_PROMPT.format(today=today)

    # Build a rich context message
    context_lines = [
        f"Deal just won — please build the production plan.",
        f"",
        f"*Deal details:*",
        f"• Name: {name}",
        f"• Value: ${value:,.0f}" if value else "• Value: unknown",
        f"• Owner: {owner or 'TBD'}",
        f"• Close/delivery date: {close.strftime('%Y-%m-%d') if close else 'not set'}",
        f"• Attio record ID: {record_id}",
        f"",
        f"Steps: infer deliverable type → check capacity → write Notion plan → return brief.",
    ]
    user_message = "\n".join(context_lines)

    messages: list[dict] = [{"role": "user", "content": user_message}]

    for turn in range(MAX_TURNS):
        logger.debug("Production planner turn %d for: %s", turn + 1, name)

        response = await _claude.messages.create(
            model=MODEL,
            max_tokens=1500,
            system=system,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            brief = _extract_text(response)
            logger.info(
                "Production planner done for %s (%d turns).", name, turn + 1
            )
            return brief or f"_Production plan created for {name}. Check Notion._"

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                logger.debug(
                    "Production planner tool: %s(%s)",
                    block.name,
                    json.dumps(block.input)[:100],
                )
                result = await _execute_tool(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, default=str),
                })

            messages.append({"role": "user", "content": tool_results})
            continue

        break  # Unexpected stop reason

    logger.warning("Production planner hit MAX_TURNS for %s.", name)
    return f"_Production planning timed out for {name}. Please create the Notion entry manually._"


def _extract_text(response) -> str:
    return "\n".join(
        block.text for block in response.content if hasattr(block, "text")
    ).strip()
