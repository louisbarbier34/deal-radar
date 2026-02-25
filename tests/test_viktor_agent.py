"""Unit tests for Viktor's intent parsing and routing logic."""
import pytest


class TestParseIntent:
    """Test intent extraction — uses mock_claude + claude_json fixtures from conftest."""

    @pytest.mark.asyncio
    async def test_update_probability(self, claude_json):
        claude_json({
            "intent": "update_deal",
            "deal_name": "Nike",
            "field": "probability",
            "new_value": "85",
            "filters": {},
            "question": None,
        })
        from agents.viktor import parse_intent
        result = await parse_intent("@Viktor update Nike to 85%")
        assert result["intent"] == "update_deal"
        assert result["deal_name"] == "Nike"
        assert result["field"] == "probability"
        assert result["new_value"] == "85"

    @pytest.mark.asyncio
    async def test_update_stage(self, claude_json):
        claude_json({
            "intent": "update_deal",
            "deal_name": "Adidas",
            "field": "stage",
            "new_value": "Proposal Sent",
            "filters": {},
            "question": None,
        })
        from agents.viktor import parse_intent
        result = await parse_intent("@Viktor move Adidas to Proposal Sent")
        assert result["intent"] == "update_deal"
        assert result["field"] == "stage"

    @pytest.mark.asyncio
    async def test_query_pipeline_with_filters(self, claude_json):
        claude_json({
            "intent": "query_pipeline",
            "deal_name": None,
            "field": None,
            "new_value": None,
            "filters": {"min_probability": 70, "month": 5, "year": 2025},
            "question": "What deals are above 70% closing in May?",
        })
        from agents.viktor import parse_intent
        result = await parse_intent("What deals are above 70% closing in May?")
        assert result["intent"] == "query_pipeline"
        assert result["filters"]["min_probability"] == 70
        assert result["filters"]["month"] == 5

    @pytest.mark.asyncio
    async def test_malformed_json_returns_unknown(self, mock_claude):
        from unittest.mock import MagicMock
        content = MagicMock()
        content.text = "not json at all"
        resp = MagicMock()
        resp.content = [content]
        mock_claude.messages.create.return_value = resp

        from agents.viktor import parse_intent
        result = await parse_intent("something random")
        assert result["intent"] == "unknown"


class TestExtractDealSignals:
    @pytest.mark.asyncio
    async def test_extracts_deal_name(self, claude_json):
        claude_json({
            "deal_name": "Nike",
            "probability_hint": 70,
            "stage_hint": "Negotiation",
            "key_signals": ["contract", "approved"],
            "suggested_note": "Nike confirmed budget and approved the SOW.",
            "action_items": ["Send contract", "Schedule kickoff"],
            "urgency": "high",
        })
        from agents.viktor import extract_deal_signals_from_text
        result = await extract_deal_signals_from_text(
            "Nike confirmed budget and approved the SOW."
        )
        assert result["deal_name"] == "Nike"
        assert "contract" in result["key_signals"]
        assert result["urgency"] == "high"

    @pytest.mark.asyncio
    async def test_no_deal_name_returns_empty(self, claude_json):
        claude_json({
            "deal_name": None,
            "key_signals": [],
            "suggested_note": "",
            "urgency": "low",
        })
        from agents.viktor import extract_deal_signals_from_text
        result = await extract_deal_signals_from_text("Had lunch today.")
        assert result["deal_name"] is None


class TestAnswerPipelineQuestion:
    @pytest.mark.asyncio
    async def test_returns_formatted_answer(self, claude_text):
        claude_text("*Nike* closes May 31 at 85% — your strongest deal this month.")
        from agents.viktor import answer_pipeline_question
        result = await answer_pipeline_question("What's our best deal?", [])
        assert "Nike" in result

    @pytest.mark.asyncio
    async def test_generates_handoff_brief(self, claude_text):
        claude_text("*Project Overview*\nClient: Nike, Value: $100k")
        deal = {
            "id": {"record_id": "d1"},
            "values": {
                "name": [{"value": "Nike"}],
                "stage": [{"option": {"title": "Won"}}],
                "probability": [{"value": 100.0}],
                "value": [{"value": 100000.0}],
                "close_date": [{"value": "2025-05-31"}],
                "owner": [{"value": "Louis"}],
            },
        }
        from agents.viktor import generate_production_handoff_brief
        result = await generate_production_handoff_brief(deal)
        assert "Nike" in result or "Project Overview" in result
