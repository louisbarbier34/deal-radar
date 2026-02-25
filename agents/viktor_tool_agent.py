"""
Viktor Tool Agent — Claude with real tools.

Unlike the original viktor.py (parse intent → Python handler executes),
this agent gives Claude direct access to Attio and Slack tools and runs
a full agentic loop until it's done.

Claude decides:
  - Which tool(s) to call
  - In what order
  - Whether to ask a clarifying question
  - When it's finished

The loop is capped at MAX_TURNS to prevent runaway.

Usage (from main.py app_mention handler):
    from agents.viktor_tool_agent import run_viktor
    await run_viktor(user_message, say, slack_client, user_id=event["user"])
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import anthropic

import config
from clients.attio import attio, AttioClient
from clients.notion import notion_db
from clients.gcal import gcal
from clients.gmail import gmail

logger = logging.getLogger(__name__)

MAX_TURNS = 8
MODEL = "claude-sonnet-4-6"

_claude = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

# ─── System prompt ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are Rabbit, Wonder Studios' connected deal intelligence agent — always live, never guessing.

## Systems You Have Access To
• *Attio CRM* — deals, stages, probabilities, values, close dates, notes. Source of truth for pipeline.
• *Notion Production Calendar* — active projects, deliverable types, status, projected start, duration, crew assignments.
• *Gmail* — recent client emails: contracts, SOWs, budget confirmations, deal signals, client sentiment.
• *Google Calendar* — upcoming prospect/client meetings: title, attendees, start time, Meet link.

## Tool Strategy
- *Always call a tool before answering* — never respond from memory when live data is available
- *Pipeline / deal questions* → search_deals first; add get_pipeline_summary for totals
- *Production / capacity questions* → search_deals (pipeline) + search_notion_production_calendar (active projects)
- *"What's in production?"* → search_notion_production_calendar, no Attio call needed
- *Client outreach / email questions* → get_recent_email_signals; cross-check Attio if a deal is mentioned
- *Meeting / schedule questions* → get_upcoming_meetings
- *Updating a deal* → always search_deals first to confirm record_id, then update_deal_field
- *Cross-system questions* (e.g. "how's Nike doing?") → search_deals + get_recent_email_signals + search_notion in parallel turns

## Output Rules
- Slack markdown only: *bold*, _italic_, `code`, bullet points — no HTML, no headers with #
- Be concise — Slack messages, not documents
- After any write (update, note, Notion entry) → confirm: deal name, field changed, new value
- If multiple deals match a name → list them and ask which one before acting
- If a tool returns an error → say so clearly, suggest the user check manually
- Today: {today}"""

# ─── Tool definitions (Claude function-calling schema) ────────────────────────

TOOLS: list[dict] = [
    {
        "name": "search_deals",
        "description": (
            "Search deals in Attio CRM. Returns matching deals with name, stage, "
            "probability, value, close date, and owner. Use this before updating "
            "to confirm the deal exists and get its record ID."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name_query": {
                    "type": "string",
                    "description": "Partial or full deal/client name to search for. Leave empty to get all active deals.",
                },
                "min_probability": {
                    "type": "number",
                    "description": "Minimum win probability (0-100). Optional.",
                },
                "max_probability": {
                    "type": "number",
                    "description": "Maximum win probability (0-100). Optional.",
                },
                "stage": {
                    "type": "string",
                    "description": "Filter by stage name (e.g. 'Negotiation', 'Proposal Sent'). Optional.",
                },
                "closing_month": {
                    "type": "integer",
                    "description": "Filter by close month 1-12. Optional.",
                },
                "closing_year": {
                    "type": "integer",
                    "description": "Filter by close year (e.g. 2025). Optional.",
                },
                "include_closed": {
                    "type": "boolean",
                    "description": "Include Won/Lost deals. Default false.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "update_deal_field",
        "description": (
            "Update a single field on a deal in Attio. "
            "Always search_deals first to confirm the record_id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "record_id": {
                    "type": "string",
                    "description": "The Attio record ID of the deal to update.",
                },
                "field": {
                    "type": "string",
                    "enum": ["probability", "stage", "close_date", "value"],
                    "description": "The field to update.",
                },
                "value": {
                    "type": "string",
                    "description": (
                        "New value. For probability: number 0-100. "
                        "For stage: stage name string. "
                        "For close_date: ISO date string YYYY-MM-DD. "
                        "For value: number."
                    ),
                },
            },
            "required": ["record_id", "field", "value"],
        },
    },
    {
        "name": "add_note_to_deal",
        "description": "Add a text note to a deal record in Attio.",
        "input_schema": {
            "type": "object",
            "properties": {
                "record_id": {
                    "type": "string",
                    "description": "The Attio record ID of the deal.",
                },
                "title": {
                    "type": "string",
                    "description": "Short title for the note (e.g. 'Call recap', 'Deal update').",
                },
                "body": {
                    "type": "string",
                    "description": "Full note content.",
                },
            },
            "required": ["record_id", "title", "body"],
        },
    },
    {
        "name": "get_pipeline_summary",
        "description": (
            "Get a high-level pipeline summary: total active deals, "
            "weighted value, deals by stage, and upcoming close dates."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "months_ahead": {
                    "type": "integer",
                    "description": "How many months ahead to include in close date summary. Default 3.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_capacity_analysis",
        "description": (
            "Analyse production capacity: which months have multiple high-probability "
            "deals targeting the same production window. Returns conflict summary."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "min_probability": {
                    "type": "number",
                    "description": "Probability threshold to count a deal as 'likely'. Default 60.",
                },
            },
            "required": [],
        },
    },
    # ── Notion ────────────────────────────────────────────────────────────────
    {
        "name": "search_notion_production_calendar",
        "description": (
            "Search the Notion Production Calendar for active projects. "
            "Returns project name, deliverable type, production status, "
            "projected start, duration, deal value, and assigned production lead. "
            "Use for questions about what's currently in production, delivery schedules, "
            "or crew assignments."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name_query": {
                    "type": "string",
                    "description": "Optional project or client name filter.",
                },
                "status_filter": {
                    "type": "string",
                    "description": (
                        "Optional status filter. One of: "
                        "'Pre-Production', 'In Production', 'Post', "
                        "'Delivered', 'Handed Off', 'On Hold'."
                    ),
                },
            },
            "required": [],
        },
    },
    # ── Google Calendar ───────────────────────────────────────────────────────
    {
        "name": "get_upcoming_meetings",
        "description": (
            "Fetch upcoming prospect/client meetings from Google Calendar. "
            "Returns title, start time, external attendees, organiser, "
            "minutes until start, and Meet link. "
            "Use for questions like 'what calls do I have tomorrow?' or "
            "'who am I meeting with this week?'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hours_ahead": {
                    "type": "integer",
                    "description": "How many hours ahead to look. Default 48 (2 days).",
                },
            },
            "required": [],
        },
    },
    # ── Gmail ─────────────────────────────────────────────────────────────────
    {
        "name": "get_recent_email_signals",
        "description": (
            "Scan recent client emails for deal signals: contract language, SOW references, "
            "budget confirmations, timeline shifts, meeting confirmations. "
            "Use for questions like 'any emails from Nike?' or "
            "'did we hear back about the Adidas proposal?' or "
            "'what deal signals came in this week?'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hours_back": {
                    "type": "integer",
                    "description": "How many hours back to scan. Default 48.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of emails to return (1-20). Default 10.",
                },
            },
            "required": [],
        },
    },
]

# ─── Tool executor ────────────────────────────────────────────────────────────

async def _execute_tool(name: str, inputs: dict) -> Any:
    """Dispatch a Claude tool call to the correct implementation."""
    try:
        if name == "search_deals":
            return await _tool_search_deals(**inputs)
        elif name == "update_deal_field":
            return await _tool_update_deal(**inputs)
        elif name == "add_note_to_deal":
            return await _tool_add_note(**inputs)
        elif name == "get_pipeline_summary":
            return await _tool_pipeline_summary(**inputs)
        elif name == "get_capacity_analysis":
            return await _tool_capacity_analysis(**inputs)
        elif name == "search_notion_production_calendar":
            return await _tool_search_notion(**inputs)
        elif name == "get_upcoming_meetings":
            return await _tool_get_meetings(**inputs)
        elif name == "get_recent_email_signals":
            return await _tool_get_email_signals(**inputs)
        else:
            return {"error": f"Unknown tool: {name}"}
    except Exception as exc:
        logger.error("Tool %s failed: %s", name, exc)
        return {"error": str(exc)}


async def _tool_search_deals(
    name_query: str = "",
    min_probability: float | None = None,
    max_probability: float | None = None,
    stage: str | None = None,
    closing_month: int | None = None,
    closing_year: int | None = None,
    include_closed: bool = False,
) -> list[dict]:
    deals = await attio.list_deals()
    closed_stages = {"closed won", "won", "closed lost", "lost"}

    results = []
    for d in deals:
        d_stage = AttioClient._deal_stage(d).lower()
        d_prob = AttioClient._deal_probability(d)
        d_close = AttioClient._deal_close_date(d)
        d_name = AttioClient._deal_name(d).lower()

        if not include_closed and d_stage in closed_stages:
            continue
        if name_query and name_query.lower() not in d_name:
            continue
        if min_probability is not None and (d_prob is None or d_prob < min_probability):
            continue
        if max_probability is not None and (d_prob is None or d_prob > max_probability):
            continue
        if stage and stage.lower() not in d_stage:
            continue
        if closing_month is not None and (d_close is None or d_close.month != closing_month):
            continue
        if closing_year is not None and (d_close is None or d_close.year != closing_year):
            continue

        close_str = d_close.strftime("%Y-%m-%d") if d_close else None
        results.append({
            "record_id": d.get("id", {}).get("record_id", ""),
            "name": AttioClient._deal_name(d),
            "stage": AttioClient._deal_stage(d),
            "probability": d_prob,
            "value": AttioClient._deal_value(d),
            "close_date": close_str,
            "owner": AttioClient._deal_owner(d),
        })

    return results[:20]  # cap at 20 to keep context manageable


async def _tool_update_deal(record_id: str, field: str, value: str) -> dict:
    field_map = {
        "probability": lambda v: {"probability": float(v)},
        "stage": lambda v: {"stage": str(v)},
        "close_date": lambda v: {"close_date": str(v)},
        "value": lambda v: {"value": float(v)},
    }
    if field not in field_map:
        return {"error": f"Unknown field: {field}"}

    try:
        attrs = field_map[field](value)
    except (ValueError, TypeError) as exc:
        return {"error": f"Invalid value for {field}: {exc}"}

    await attio.update_deal(record_id, attrs)
    return {"success": True, "record_id": record_id, "field": field, "new_value": value}


async def _tool_add_note(record_id: str, title: str, body: str) -> dict:
    await attio.add_note(record_id, title, body)
    return {"success": True, "record_id": record_id}


async def _tool_pipeline_summary(months_ahead: int = 3) -> dict:
    now = datetime.now(timezone.utc)
    active = await attio.get_active_deals()

    total_value = sum(AttioClient._deal_value(d) or 0 for d in active)
    weighted = sum(
        (AttioClient._deal_value(d) or 0) * (AttioClient._deal_probability(d) or 0) / 100
        for d in active
    )

    by_stage: dict[str, int] = {}
    closing_soon: list[dict] = []

    for d in active:
        stage = AttioClient._deal_stage(d) or "Unknown"
        by_stage[stage] = by_stage.get(stage, 0) + 1

        close = AttioClient._deal_close_date(d)
        if close:
            months_diff = (close.year - now.year) * 12 + (close.month - now.month)
            if 0 <= months_diff <= months_ahead:
                closing_soon.append({
                    "name": AttioClient._deal_name(d),
                    "probability": AttioClient._deal_probability(d),
                    "value": AttioClient._deal_value(d),
                    "close_date": close.strftime("%Y-%m-%d"),
                })

    closing_soon.sort(key=lambda x: x["close_date"])

    return {
        "active_deal_count": len(active),
        "total_pipeline_value": round(total_value, 2),
        "weighted_value": round(weighted, 2),
        "deals_by_stage": by_stage,
        "closing_in_next_months": closing_soon,
    }


async def _tool_capacity_analysis(min_probability: float = 60) -> dict:
    from collections import defaultdict
    active = await attio.get_active_deals()
    now = datetime.now(timezone.utc)
    by_month: dict[str, list] = defaultdict(list)

    for d in active:
        prob = AttioClient._deal_probability(d) or 0
        close = AttioClient._deal_close_date(d)
        if prob >= min_probability and close and (close.year, close.month) >= (now.year, now.month):
            key = close.strftime("%B %Y")
            by_month[key].append({
                "name": AttioClient._deal_name(d),
                "probability": prob,
                "value": AttioClient._deal_value(d),
            })

    conflicts = {m: deals for m, deals in by_month.items() if len(deals) >= 2}
    safe = {m: deals for m, deals in by_month.items() if len(deals) < 2}

    return {
        "conflict_months": conflicts,
        "safe_months": safe,
        "threshold_probability": min_probability,
    }


# ─── New tool implementations: Notion / GCal / Gmail ─────────────────────────

def _parse_notion_page(page: dict) -> dict:
    """Extract readable fields from a raw Notion page API response."""
    props = page.get("properties", {})

    def _title(p: dict) -> str:
        return "".join(t.get("text", {}).get("content", "") for t in p.get("title", []))

    def _rich_text(p: dict) -> str:
        return "".join(t.get("text", {}).get("content", "") for t in p.get("rich_text", []))

    def _select(p: dict) -> str | None:
        s = p.get("select")
        return s.get("name") if s else None

    def _date(p: dict) -> str | None:
        d = p.get("date")
        return d.get("start") if d else None

    return {
        "name": _title(props.get("Project Name", {})),
        "client": _rich_text(props.get("Client", {})),
        "deliverable_type": _select(props.get("Deliverable Type", {})),
        "production_status": _select(props.get("Production Status", {})),
        "stage": _select(props.get("Stage", {})),
        "close_date": _date(props.get("Close Date", {})),
        "projected_start": _date(props.get("Projected Start", {})),
        "duration_weeks": props.get("Duration (weeks)", {}).get("number"),
        "deal_value": props.get("Deal Value", {}).get("number"),
        "production_lead": _rich_text(props.get("Production Lead", {})),
        "attio_record_id": _rich_text(props.get("Attio Record ID", {})),
    }


async def _tool_search_notion(
    name_query: str = "",
    status_filter: str = "",
) -> list[dict]:
    """Return matching projects from the Notion Production Calendar."""
    pages = await notion_db.get_all_pages()
    results = []
    for page in pages:
        parsed = _parse_notion_page(page)
        if name_query and name_query.lower() not in (parsed["name"] or "").lower():
            continue
        if status_filter and status_filter.lower() != (parsed["production_status"] or "").lower():
            continue
        results.append(parsed)
    return results[:15]  # keep context manageable


async def _tool_get_meetings(hours_ahead: int = 48) -> list[dict]:
    """Return upcoming prospect/client meetings from Google Calendar."""
    try:
        meetings = gcal.get_upcoming_prospect_meetings(hours_ahead=hours_ahead)
    except Exception as exc:
        return [{"error": str(exc)}]

    return [
        {
            "title": m.get("title", ""),
            "start": m["start"].strftime("%A %b %d, %I:%M %p") if m.get("start") else "",
            "minutes_until_start": m.get("minutes_until_start"),
            "external_attendees": m.get("external_attendees", []),
            "organizer": m.get("organizer", ""),
            "meet_link": m.get("meet_link", ""),
        }
        for m in meetings
    ]


async def _tool_get_email_signals(
    hours_back: int = 48,
    max_results: int = 10,
) -> list[dict]:
    """Return recent deal-signal emails from Gmail."""
    from datetime import timedelta
    since = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    try:
        signals = gmail.scan_for_deal_signals(
            after_timestamp=since,
            max_results=min(max_results, 20),
        )
    except Exception as exc:
        return [{"error": str(exc)}]

    return [
        {
            "sender": s.get("sender", ""),
            "subject": s.get("subject", ""),
            "date": s["date"].strftime("%b %d %I:%M %p") if s.get("date") else "",
            "snippet": s.get("snippet", "")[:300],
            "matched_keywords": s.get("matched_keywords", []),
        }
        for s in signals
    ]


# ─── Main agent loop ──────────────────────────────────────────────────────────

async def run_viktor(
    message: str,
    say,
    slack_client=None,
    user_id: str = "",
    thread_ts: str | None = None,
) -> None:
    """
    Run the Viktor agentic loop.

    Claude receives the user's message and a full tool suite.
    It calls tools as needed, iterates, and posts its final response.
    """
    today = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
    system = SYSTEM_PROMPT.format(today=today)

    messages: list[dict] = [
        {"role": "user", "content": message}
    ]

    say_kwargs: dict = {}
    if thread_ts:
        say_kwargs["thread_ts"] = thread_ts

    for turn in range(MAX_TURNS):
        logger.info("Viktor turn %d/%d", turn + 1, MAX_TURNS)

        response = await _claude.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=system,
            tools=TOOLS,
            messages=messages,
        )

        # ── End turn: Claude is done, post final response ─────────────
        if response.stop_reason == "end_turn":
            text = _extract_text(response)
            if text:
                await say(text=text, **say_kwargs)
            logger.info("Viktor finished in %d turns.", turn + 1)
            return

        # ── Tool use: execute each tool call, feed results back ────────
        if response.stop_reason == "tool_use":
            # Append Claude's assistant message (contains tool_use blocks)
            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                logger.info("Viktor calling tool: %s(%s)", block.name, json.dumps(block.input)[:120])
                result = await _execute_tool(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, default=str),
                })

            messages.append({"role": "user", "content": tool_results})
            continue

        # ── Unexpected stop reason ─────────────────────────────────────
        logger.warning("Viktor: unexpected stop_reason=%s", response.stop_reason)
        break

    # Exceeded MAX_TURNS
    await say(
        text="_I got a bit turned around on that one. Could you rephrase or be more specific?_",
        **say_kwargs,
    )
    logger.warning("Viktor hit MAX_TURNS (%d) without finishing.", MAX_TURNS)


def _extract_text(response) -> str:
    """Pull plain text from a Claude response (ignoring tool_use blocks)."""
    parts = []
    for block in response.content:
        if hasattr(block, "text"):
            parts.append(block.text)
    return "\n".join(parts).strip()
