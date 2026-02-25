"""
Viktor — the Claude-powered deal intelligence agent.

Viktor understands natural language commands and queries about the pipeline.
All team interactions with Viktor go through this module.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import anthropic

import config
from clients.attio import attio, AttioClient

logger = logging.getLogger(__name__)

# Async client — non-blocking, event loop stays free during API calls
_claude = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

# ─── System prompt ────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are Rabbit, the deal intelligence assistant for Wonder Studios.
Wonder Studios is a production company. You help the sales and production team
stay on top of their pipeline in Attio CRM.

Your job:
- Parse natural language commands and queries about deals
- Extract structured intent: update requests, data queries, forecasts
- Return concise, actionable answers in Slack-friendly text
- Always be specific with numbers, dates, and deal names
- Use bullet points for lists, bold for deal names

Tone: direct, professional, no fluff. You're a sharp internal tool, not a chatbot.

When you receive deal data as JSON context, use it to answer questions accurately.
Never fabricate deal names, probabilities, or values — only reference what you're given.

Output format: plain Slack markdown (bold = *text*, italic = _text_, no HTML).
"""

# ─── Intent parsing ───────────────────────────────────────────────────────────

INTENT_EXTRACTION_PROMPT = """Analyze this message from a Wonder Studios team member.

Message: {message}

Extract the intent as JSON with this exact schema:
{{
  "intent": "update_deal" | "query_pipeline" | "query_deal" | "forecast" | "capacity_check" | "unknown",
  "deal_name": "<string or null>",
  "field": "<probability|stage|close_date|value|note|null>",
  "new_value": "<string or null>",
  "filters": {{
    "min_probability": <number or null>,
    "max_probability": <number or null>,
    "month": <1-12 or null>,
    "year": <number or null>,
    "stage": "<string or null>",
    "owner": "<string or null>"
  }},
  "question": "<rephrased natural language question or null>"
}}

Examples:
- "@Viktor update Nike to 85%" → intent=update_deal, deal_name=Nike, field=probability, new_value=85
- "@Viktor what deals are above 70% closing in May?" → intent=query_pipeline, filters.min_probability=70, filters.month=5
- "@Viktor forecast for Q2" → intent=forecast, filters.month covers Q2 months
- "@Viktor move Nike to Proposal Sent" → intent=update_deal, deal_name=Nike, field=stage, new_value=Proposal Sent

Return ONLY the JSON, no explanation."""


async def parse_intent(message: str) -> dict:
    """Use Claude to parse a Viktor command into structured intent."""
    prompt = INTENT_EXTRACTION_PROMPT.format(message=message)
    try:
        response = await _claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system="You extract structured JSON from natural language. Return only valid JSON.",
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except (json.JSONDecodeError, Exception) as exc:
        logger.warning("Intent parsing failed: %s", exc)
        return {"intent": "unknown", "question": message}


# ─── Natural language query answering ────────────────────────────────────────

async def answer_pipeline_question(question: str, deals: list[dict]) -> str:
    """
    Given a natural language question and a list of deal dicts,
    ask Claude to answer it in Slack markdown.
    """
    deals_json = json.dumps(
        [_deal_summary(d) for d in deals[:50]],  # cap context size
        indent=2,
    )

    messages = [
        {
            "role": "user",
            "content": (
                f"Here is the current pipeline data:\n```json\n{deals_json}\n```\n\n"
                f"Question: {question}\n\n"
                "Answer concisely in Slack markdown. Be specific with numbers."
            ),
        }
    ]

    response = await _claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=messages,
    )
    return response.content[0].text.strip()


async def extract_deal_signals_from_text(text: str, context: str = "") -> dict:
    """
    Extract deal signals from free text (meeting notes, Slack messages, emails).
    Returns structured dict: {deal_name, probability, stage, notes, action_items}
    """
    prompt = f"""Analyze this text for deal signals relevant to Wonder Studios' pipeline.

Text: {text}
{f'Context: {context}' if context else ''}

Extract as JSON:
{{
  "deal_name": "<company/client name or null>",
  "probability_hint": <0-100 or null>,
  "stage_hint": "<Lead|Qualified|Proposal Sent|Negotiation|Won|Lost or null>",
  "key_signals": ["<signal 1>", "..."],
  "suggested_note": "<2-3 sentence summary suitable for Attio note>",
  "action_items": ["<action 1>", "..."],
  "urgency": "high|medium|low"
}}

Return only valid JSON."""

    try:
        response = await _claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system="You extract CRM-relevant signals from business communications. Return only JSON.",
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except Exception as exc:
        logger.warning("Signal extraction failed: %s", exc)
        return {"deal_name": None, "key_signals": [], "suggested_note": text[:500]}


async def generate_production_handoff_brief(deal: dict) -> str:
    """Generate a production handoff brief for a newly-won deal."""
    summary = _deal_summary(deal)
    prompt = f"""A deal has been marked Won at Wonder Studios. Generate a production handoff brief.

Deal data:
{json.dumps(summary, indent=2)}

Write a brief for the production team in Slack markdown. Include:
1. *Project Overview* — client, deliverable, value, timeline
2. *Key Contacts* — who the deal owner is
3. *Production Notes* — any relevant context from the deal
4. *Immediate Next Steps* — 3 specific action items for production

Keep it punchy and scannable. Use Slack markdown (*bold*, _italic_, bullet points)."""

    response = await _claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


async def generate_monday_forecast_narrative(
    deals: list[dict],
    pipeline_value: float,
    weighted_value: float,
) -> str:
    """Generate the written narrative for the Monday forecast post."""
    deals_json = json.dumps([_deal_summary(d) for d in deals[:30]], indent=2)
    prompt = f"""Write the Monday pipeline narrative for Wonder Studios' #deal-radar digest.

Pipeline snapshot:
{deals_json}

Total pipeline: ${pipeline_value:,.0f} · Weighted: ${weighted_value:,.0f} · {len(deals)} active deal(s)

Write exactly 2-3 sentences covering:
1. Overall pipeline health (use the weighted value as the key signal)
2. One specific deal to watch this week (highest probability or nearest close date)
3. One risk or opportunity worth flagging (stale deal, capacity crunch, or strong momentum)

Rules:
- Slack markdown only (*bold* for deal names, _italic_ for emphasis)
- Under 65 words — tight and punchy, not a report
- Use specific numbers and deal names — no vague commentary
- Do not start with "The pipeline" — vary the opening"""

    response = await _claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _deal_summary(deal: dict) -> dict:
    """Compact serializable summary of a deal for Claude context."""
    close = AttioClient._deal_close_date(deal)
    return {
        "name": AttioClient._deal_name(deal),
        "stage": AttioClient._deal_stage(deal),
        "probability": AttioClient._deal_probability(deal),
        "value": AttioClient._deal_value(deal),
        "close_date": close.strftime("%Y-%m-%d") if close else None,
        "owner": AttioClient._deal_owner(deal),
        "record_id": deal.get("id", {}).get("record_id", ""),
    }
