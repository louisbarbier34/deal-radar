"""
Research Agent — Pre-meeting prospect intelligence.

Given a company or person name (from a calendar event), researches them
and returns a formatted brief for the deal owner's Slack DM.

Steps the agent takes (via tool use):
  1. Search Attio for existing deal history
  2. Web-search recent company news / funding / campaigns
  3. Synthesise into a concise pre-meeting brief

Usage:
    from agents.research_agent import run_research_agent
    brief = await run_research_agent(
        company_name="Nike",
        meeting_title="Nike Q2 scope call",
        attendees=["sarah@nike.com"],
    )
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import anthropic
import httpx
from duckduckgo_search import DDGS

import config
from clients.attio import attio, AttioClient

logger = logging.getLogger(__name__)

MAX_TURNS = 6
MODEL = "claude-sonnet-4-6"

_claude = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

# ─── System prompt ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are Rabbit, pre-meeting intelligence agent for Wonder Studios — a VFX and production company.

Your job: produce a sharp, sub-200-word brief the deal owner reads in 30 seconds before a call.

## Research Process (always follow in order)
1. *CRM first* — call get_attio_deal_history with the company name. Gives deal stage, value, owner, last activity.
2. *Web search* — call web_search("{company} production campaign 2026") or "{company} brand content launch". Focus on what's relevant to a production pitch: new campaigns, launch plans, budget announcements, content spend.
3. *Freshness pass* — call get_company_news with the company name to surface anything from the past 7 days.

## Output Format  (Slack markdown, strictly < 200 words)
*Pre-Meeting Brief: {company}*

*Deal Context*
• Stage · Probability · $Value · Last activity
(If no Attio deal found, write: _No active deal — new prospect_)

*Recent Intel*
• 2-3 bullets of news/campaigns/moves — production-angle only, no general press

*Smart Angles*
• 1-2 specific conversation hooks based on what you found (reference real findings)

## Rules
- NEVER skip tools — always fetch live data before writing a single word of the brief
- If Attio returns no match, still complete Intel and Angles from web research
- Never fabricate deal values, probabilities, or dates
- Do not pad — if there are no recent findings, say so clearly in one bullet
- Do not repeat the company name in every bullet
- Today: {today}"""

# ─── Tools ────────────────────────────────────────────────────────────────────

TOOLS: list[dict] = [
    {
        "name": "get_attio_deal_history",
        "description": (
            "Look up existing deals and notes in Attio CRM for a company. "
            "Returns deal status, probability, value, stage, owner, and last activity."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "company_name": {
                    "type": "string",
                    "description": "Company or client name to search for.",
                },
            },
            "required": ["company_name"],
        },
    },
    {
        "name": "web_search",
        "description": (
            "Search the web for recent news, campaigns, or business intelligence "
            "about a company. Returns up to 5 relevant results."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query, e.g. 'Nike 2025 brand campaign production'",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Number of results to return (1-5). Default 4.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_company_news",
        "description": (
            "Fetch recent news headlines for a company from DuckDuckGo News. "
            "More targeted than web_search for breaking news."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "company_name": {
                    "type": "string",
                    "description": "Company name to search news for.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Number of news items (1-5). Default 3.",
                },
            },
            "required": ["company_name"],
        },
    },
]

# ─── Tool implementations ─────────────────────────────────────────────────────

async def _tool_get_attio_history(company_name: str) -> dict:
    deals = await attio.list_deals()
    matched = [
        d for d in deals
        if company_name.lower() in AttioClient._deal_name(d).lower()
    ]

    if not matched:
        return {"found": False, "company": company_name}

    results = []
    for d in matched[:3]:
        close = AttioClient._deal_close_date(d)
        last_updated = AttioClient._deal_last_updated(d)
        results.append({
            "name": AttioClient._deal_name(d),
            "stage": AttioClient._deal_stage(d),
            "probability": AttioClient._deal_probability(d),
            "value": AttioClient._deal_value(d),
            "close_date": close.strftime("%Y-%m-%d") if close else None,
            "owner": AttioClient._deal_owner(d),
            "last_activity": (
                f"{(datetime.now(timezone.utc) - last_updated).days} days ago"
                if last_updated else "unknown"
            ),
            "record_id": d.get("id", {}).get("record_id", ""),
        })

    return {"found": True, "deals": results}


def _tool_web_search(query: str, max_results: int = 4) -> list[dict]:
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=min(max_results, 5)))
        return [
            {"title": r.get("title", ""), "snippet": r.get("body", "")[:300], "url": r.get("href", "")}
            for r in results
        ]
    except Exception as exc:
        logger.warning("Web search failed: %s", exc)
        return [{"error": str(exc)}]


def _tool_get_company_news(company_name: str, max_results: int = 3) -> list[dict]:
    try:
        with DDGS() as ddgs:
            results = list(ddgs.news(company_name, max_results=min(max_results, 5)))
        return [
            {
                "title": r.get("title", ""),
                "snippet": r.get("body", "")[:250],
                "source": r.get("source", ""),
                "date": r.get("date", ""),
            }
            for r in results
        ]
    except Exception as exc:
        logger.warning("News search failed: %s", exc)
        return [{"error": str(exc)}]


async def _execute_tool(name: str, inputs: dict) -> Any:
    try:
        if name == "get_attio_deal_history":
            return await _tool_get_attio_history(**inputs)
        elif name == "web_search":
            return _tool_web_search(**inputs)
        elif name == "get_company_news":
            return _tool_get_company_news(**inputs)
        else:
            return {"error": f"Unknown tool: {name}"}
    except Exception as exc:
        logger.error("Research tool %s failed: %s", name, exc)
        return {"error": str(exc)}

# ─── Main agent ───────────────────────────────────────────────────────────────

async def run_research_agent(
    company_name: str,
    meeting_title: str = "",
    attendees: list[str] | None = None,
    minutes_until_meeting: int = 60,
) -> str:
    """
    Run the research agent for a prospect company.
    Returns a Slack-formatted pre-meeting brief string.
    """
    today = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
    system = SYSTEM_PROMPT.format(company=company_name, today=today)

    context_parts = [f"Company: {company_name}"]
    if meeting_title:
        context_parts.append(f"Meeting: {meeting_title}")
    if attendees:
        context_parts.append(f"Attendees: {', '.join(attendees[:3])}")
    context_parts.append(f"Meeting in: {minutes_until_meeting} minutes")

    user_message = (
        "Research this prospect and build a pre-meeting brief.\n\n"
        + "\n".join(context_parts)
    )

    messages: list[dict] = [{"role": "user", "content": user_message}]

    for turn in range(MAX_TURNS):
        logger.debug("Research agent turn %d for: %s", turn + 1, company_name)

        response = await _claude.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=system,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            brief = _extract_text(response)
            logger.info("Research agent done for %s (%d turns).", company_name, turn + 1)
            return brief or f"_No research found for {company_name}_"

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                logger.debug("Research tool: %s(%s)", block.name, json.dumps(block.input)[:80])
                result = await _execute_tool(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, default=str),
                })

            messages.append({"role": "user", "content": tool_results})
            continue

        break

    logger.warning("Research agent hit MAX_TURNS for %s.", company_name)
    return f"_Research timed out for {company_name}. Check manually._"


def _extract_text(response) -> str:
    return "\n".join(
        block.text for block in response.content if hasattr(block, "text")
    ).strip()
