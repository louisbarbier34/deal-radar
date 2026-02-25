"""
Signal Agent — Extract deal intelligence from emails and meeting recaps.

Replaces the single-shot extract_deal_signals_from_text() with a proper
agentic loop that:
  1. Extracts key signals and identifies the likely deal/company
  2. Searches Attio to find and confirm the correct deal record
  3. Handles ambiguity: returns candidates when multiple deals match
  4. Optionally logs a structured note directly to Attio (auto_log=True)

Two confidence levels drive the outcome:
  - "high"   → direct Attio logging (or strong suggestion)
  - "medium" → returns candidates for caller to surface via Slack button
  - "low"    → flags as uncertain, no logging

Usage:
    from agents.signal_agent import run_signal_agent

    # From A2 / A3 — returns structured result for Slack display:
    result = await run_signal_agent(email_text, context="Email from client@brand.com", source="email")

    # From Pipedream webhook / batch — auto-log when confident:
    result = await run_signal_agent(transcript, source="meeting_transcript", auto_log=True)

    # Result shape:
    {
        "deal_name":   str | None,
        "record_id":   str | None,
        "confidence":  "high" | "medium" | "low",
        "note_title":  str,
        "note_body":   str,
        "key_signals": list[str],
        "action_items": list[str],
        "urgency":     "high" | "medium" | "low",
        "logged":      bool,
        "candidates":  list[dict],   # populated when confidence="medium"
    }
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import anthropic

import config
from clients.attio import attio, AttioClient

logger = logging.getLogger(__name__)

MAX_TURNS = 5
MODEL = "claude-sonnet-4-6"

_claude = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

# ─── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are Rabbit, deal signal extractor for Wonder Studios — a VFX and production company.

Your job: read {source} text and extract structured CRM intelligence. Output ONLY a valid JSON object — no prose, no markdown, no explanation before or after.

## Signal Vocabulary
Strong signals → confidence=high: signed SOW · signed contract · PO issued · budget confirmed/approved · retainer agreed · project kickoff confirmed · start date locked
Medium signals → confidence=medium: verbal approval · "we'd like to move forward" · budget range discussed · narrowed to 2 vendors · conditional yes
Weak signals → confidence=low: exploratory call · RFP received · intro meeting · general interest · "let's stay in touch"

## Confidence Rules
- *high*: exactly 1 Attio deal matches the name, deal is active, AND signal is strong
- *medium*: 2+ deals match, OR company name is uncertain, OR signal is medium strength
- *low*: no Attio match, signal is weak, or deal is closed/lost

## Your Process (always follow in order)
1. Read the text — identify company/brand name and any project references
2. If NO company name is identifiable → output immediately with confidence="low", skip tools
3. Call search_deals with the company name
4. Apply confidence rules to the match results
5. If confidence=high AND auto_log=true → call log_signal_to_deal; set logged=true
6. Output the final JSON — nothing else

## Urgency Rules
- high: response/action needed today (contract deadline, imminent kickoff, deal about to expire)
- medium: action needed this week
- low: FYI / no immediate action required

## JSON Output Shape (all keys required, no extras)
{{
  "deal_name":    "<matched deal name or null>",
  "record_id":    "<Attio record_id or null>",
  "confidence":   "high|medium|low",
  "note_title":   "<concise CRM note title, e.g. 'Email — budget confirmed'>",
  "note_body":    "<2-5 sentences: what happened · what was agreed · what's next>",
  "key_signals":  ["<specific signal extracted>", ...],
  "action_items": ["<concrete next step>", ...],
  "urgency":      "high|medium|low",
  "logged":       true|false,
  "candidates":   [<deal dicts when confidence=medium, empty array otherwise>]
}}

Today: {today}"""

# ─── Tool definitions ──────────────────────────────────────────────────────────

TOOLS: list[dict] = [
    {
        "name": "search_deals",
        "description": (
            "Search Attio CRM for deals matching a company or project name. "
            "Call this to confirm the correct deal record before logging anything."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Company name, brand, or project keyword to search for.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "log_signal_to_deal",
        "description": (
            "Add a note to a confirmed deal in Attio. "
            "Only call this when you are confident (high confidence) the record_id is correct. "
            "This action cannot be undone."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "record_id": {
                    "type": "string",
                    "description": "Attio record ID of the deal to log to.",
                },
                "title": {
                    "type": "string",
                    "description": "Short note title (e.g. 'Email signal — budget confirmed').",
                },
                "body": {
                    "type": "string",
                    "description": "Full note content (2-5 sentences).",
                },
            },
            "required": ["record_id", "title", "body"],
        },
    },
]

# ─── Tool implementations ───────────────────────────────────────────────────────

async def _tool_search_deals(query: str) -> list[dict]:
    """Fuzzy search active deals by name."""
    deals = await attio.list_deals()
    query_lower = query.lower().strip()

    matches = []
    for d in deals:
        d_name = AttioClient._deal_name(d).lower()
        if query_lower in d_name or d_name in query_lower:
            matches.append({
                "record_id": d.get("id", {}).get("record_id", ""),
                "name": AttioClient._deal_name(d),
                "stage": AttioClient._deal_stage(d),
                "probability": AttioClient._deal_probability(d),
                "value": AttioClient._deal_value(d),
                "owner": AttioClient._deal_owner(d),
            })

    return matches[:10]  # cap for context size


async def _tool_log_signal(record_id: str, title: str, body: str) -> dict:
    """Add a note to an Attio deal."""
    try:
        await attio.add_note(record_id, title, body)
        return {"success": True, "record_id": record_id}
    except Exception as exc:
        logger.error("Signal agent: failed to log note to %s: %s", record_id, exc)
        return {"success": False, "error": str(exc)}


async def _execute_tool(name: str, inputs: dict) -> Any:
    try:
        if name == "search_deals":
            return await _tool_search_deals(**inputs)
        elif name == "log_signal_to_deal":
            return await _tool_log_signal(**inputs)
        else:
            return {"error": f"Unknown tool: {name}"}
    except Exception as exc:
        logger.error("Signal tool %s failed: %s", name, exc)
        return {"error": str(exc)}


# ─── Fallback result ───────────────────────────────────────────────────────────

def _empty_result() -> dict:
    return {
        "deal_name": None,
        "record_id": None,
        "confidence": "low",
        "note_title": "Signal detected",
        "note_body": "",
        "key_signals": [],
        "action_items": [],
        "urgency": "low",
        "logged": False,
        "candidates": [],
    }


def _parse_json_response(text: str) -> dict | None:
    """Extract and parse a JSON object from Claude's response text."""
    text = text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(
            l for l in lines
            if not l.startswith("```")
        ).strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        # Try to find first { ... } block
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except (json.JSONDecodeError, ValueError):
                pass
    return None


# ─── Main agent ─────────────────────────────────────────────────────────────────

async def run_signal_agent(
    text: str,
    context: str = "",
    source: str = "text",
    auto_log: bool = False,
) -> dict:
    """
    Extract deal signals from text and optionally log to Attio.

    Args:
        text:      Raw text to analyse (email body, meeting recap, Slack message).
        context:   Extra context (e.g. "Email from sarah@nike.com, Subject: Q2 scope").
        source:    One of "email", "slack_recap", "meeting_transcript", "text".
        auto_log:  If True, log directly to Attio when confidence is "high".
                   If False, return result for caller to surface via Slack buttons.

    Returns:
        Structured signal dict (see module docstring).
    """
    today = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
    system = SYSTEM_PROMPT.format(source=source, today=today)

    # Build user message
    parts = [f"Source: {source}"]
    if context:
        parts.append(f"Context: {context}")
    parts.append(f"Auto-log if confident: {auto_log}")
    parts.append("")
    parts.append("--- TEXT START ---")
    parts.append(text[:3000])   # cap to keep context manageable
    parts.append("--- TEXT END ---")
    parts.append("")
    parts.append(
        "Extract signals, search for the deal in Attio, "
        + ("log the note if confidence is high, then " if auto_log else "")
        + "output the final JSON result."
    )

    user_message = "\n".join(parts)
    messages: list[dict] = [{"role": "user", "content": user_message}]

    for turn in range(MAX_TURNS):
        logger.debug("Signal agent turn %d for source: %s", turn + 1, source)

        response = await _claude.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=system,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            raw_text = _extract_text(response)
            result = _parse_json_response(raw_text)
            if result:
                # Ensure required keys are present with safe defaults
                result.setdefault("deal_name", None)
                result.setdefault("record_id", None)
                result.setdefault("confidence", "low")
                result.setdefault("note_title", "Signal detected")
                result.setdefault("note_body", "")
                result.setdefault("key_signals", [])
                result.setdefault("action_items", [])
                result.setdefault("urgency", "low")
                result.setdefault("logged", False)
                result.setdefault("candidates", [])
                logger.info(
                    "Signal agent done (%d turns): deal=%s confidence=%s logged=%s",
                    turn + 1,
                    result.get("deal_name"),
                    result.get("confidence"),
                    result.get("logged"),
                )
                return result
            # Text response but not parseable JSON — treat as low-confidence
            logger.warning("Signal agent: response not JSON-parseable, returning empty result")
            return _empty_result()

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                logger.debug(
                    "Signal tool: %s(%s)", block.name, json.dumps(block.input)[:80]
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

    logger.warning("Signal agent hit MAX_TURNS for source: %s", source)
    return _empty_result()


def _extract_text(response) -> str:
    return "\n".join(
        block.text for block in response.content if hasattr(block, "text")
    ).strip()
