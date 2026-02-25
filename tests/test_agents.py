"""
Tests for all 4 agentic modules:
  - agents/viktor_tool_agent.py   (Rabbit main chat agent)
  - agents/research_agent.py      (pre-meeting brief)
  - agents/production_planner_agent.py (won deal → Notion plan)
  - agents/signal_agent.py        (email/recap → Attio note)

Strategy:
  - Patch _claude (AsyncAnthropic) on each agent module independently
  - Mock Attio / Notion clients to avoid real API calls
  - Test the agentic loop: end_turn path and tool_use → end_turn path
  - Test edge cases: MAX_TURNS exceeded, empty result, tool errors
"""
from __future__ import annotations

import json
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _text_block(text: str):
    """Minimal fake text block (simulates Claude TextBlock)."""
    b = MagicMock()
    b.text = text
    b.type = "text"
    # hasattr check in _extract_text
    type(b).__name__ = "TextBlock"
    return b


def _tool_use_block(tool_name: str, inputs: dict, block_id: str = "tu_001"):
    """Minimal fake tool_use block."""
    b = MagicMock()
    b.type = "tool_use"
    b.name = tool_name
    b.input = inputs
    b.id = block_id
    # No .text attribute so _extract_text skips it
    del b.text
    return b


def _response(stop_reason: str, blocks):
    """Build a fake Claude response object."""
    resp = MagicMock()
    resp.stop_reason = stop_reason
    resp.content = blocks if isinstance(blocks, list) else [blocks]
    return resp


# ═══════════════════════════════════════════════════════════════════════════════
# Viktor Tool Agent (Rabbit main agent)
# ═══════════════════════════════════════════════════════════════════════════════

class TestViktorToolAgent:
    """Tests for agents.viktor_tool_agent.run_viktor"""

    def _mock_attio_deals(self):
        """Return two fake deal dicts."""
        def _deal(name, stage="Proposal Sent", prob=70, value=50000, rid="abc"):
            return {
                "id": {"record_id": rid},
                "values": {
                    "name": [{"value": name}],
                    "stage": [{"option": {"title": stage}}],
                    "probability": [{"value": prob}],
                    "value": [{"value": value}],
                    "close_date": [{"value": "2025-06-30"}],
                    "owner": [{"value": "Louis"}],
                },
                "updated_at": "2025-05-01T10:00:00Z",
            }
        return [_deal("Nike Q2 TVC", rid="rid_nike"), _deal("Adidas Campaign", rid="rid_adidas")]

    @pytest.mark.asyncio
    async def test_end_turn_on_first_response(self):
        """Agent should post and return on first end_turn."""
        say = AsyncMock()

        with patch("agents.viktor_tool_agent._claude") as mock_claude:
            mock_claude.messages.create = AsyncMock(
                return_value=_response("end_turn", [_text_block("Here is your pipeline summary.")])
            )
            from agents.viktor_tool_agent import run_viktor
            await run_viktor("show me all deals", say)

        say.assert_called_once()
        assert "pipeline summary" in say.call_args.kwargs.get("text", "")

    @pytest.mark.asyncio
    async def test_tool_use_then_end_turn(self):
        """Agent should call a tool, receive result, then post final answer."""
        say = AsyncMock()
        deals_json = json.dumps([{"record_id": "rid_nike", "name": "Nike", "stage": "Proposal Sent"}])

        responses = [
            # Turn 1: call search_deals tool
            _response("tool_use", [_tool_use_block("search_deals", {"name_query": "Nike"})]),
            # Turn 2: answer with the result
            _response("end_turn", [_text_block("*Nike* is in Proposal Sent at 70%.")]),
        ]

        with patch("agents.viktor_tool_agent._claude") as mock_claude, \
             patch("agents.viktor_tool_agent._tool_search_deals", new_callable=AsyncMock) as mock_search:
            mock_claude.messages.create = AsyncMock(side_effect=responses)
            mock_search.return_value = [{"name": "Nike", "stage": "Proposal Sent", "probability": 70}]

            from agents.viktor_tool_agent import run_viktor
            await run_viktor("what's the status of Nike?", say)

        say.assert_called_once()
        assert "Nike" in say.call_args.kwargs.get("text", "")

    @pytest.mark.asyncio
    async def test_max_turns_fallback(self):
        """When MAX_TURNS is exhausted, agent posts the fallback message."""
        say = AsyncMock()

        # Always return tool_use — never end_turn
        with patch("agents.viktor_tool_agent._claude") as mock_claude, \
             patch("agents.viktor_tool_agent._tool_search_deals", new_callable=AsyncMock) as mock_search:
            mock_claude.messages.create = AsyncMock(
                return_value=_response("tool_use", [_tool_use_block("search_deals", {"name_query": "X"})])
            )
            mock_search.return_value = []

            from agents.viktor_tool_agent import run_viktor
            await run_viktor("update something impossible", say)

        say.assert_called_once()
        assert "turned around" in say.call_args.kwargs.get("text", "").lower() or \
               say.call_args.args  # fallback message was posted

    @pytest.mark.asyncio
    async def test_thread_ts_passed_to_say(self):
        """Thread TS should be forwarded to the say call."""
        say = AsyncMock()

        with patch("agents.viktor_tool_agent._claude") as mock_claude:
            mock_claude.messages.create = AsyncMock(
                return_value=_response("end_turn", [_text_block("Done.")])
            )
            from agents.viktor_tool_agent import run_viktor
            await run_viktor("hello", say, thread_ts="12345.678")

        assert say.call_args.kwargs.get("thread_ts") == "12345.678"


# ═══════════════════════════════════════════════════════════════════════════════
# Research Agent
# ═══════════════════════════════════════════════════════════════════════════════

class TestResearchAgent:
    """Tests for agents.research_agent.run_research_agent"""

    @pytest.mark.asyncio
    async def test_returns_brief_on_end_turn(self):
        """Should return Claude's text response as the brief."""
        with patch("agents.research_agent._claude") as mock_claude:
            mock_claude.messages.create = AsyncMock(
                return_value=_response("end_turn", [_text_block("*Pre-Meeting Brief: Nike*\n\nDeal Context...")])
            )
            from agents.research_agent import run_research_agent
            brief = await run_research_agent("Nike", meeting_title="Nike Q2 scope call")

        assert "Pre-Meeting Brief" in brief
        assert "Nike" in brief

    @pytest.mark.asyncio
    async def test_web_search_tool_called(self):
        """Research agent should call web_search tool and continue loop."""
        responses = [
            _response("tool_use", [_tool_use_block("web_search", {"query": "Nike 2025 campaign"})]),
            _response("end_turn", [_text_block("*Pre-Meeting Brief: Nike*\n\n• Launched new Just Do It campaign.")]),
        ]

        with patch("agents.research_agent._claude") as mock_claude, \
             patch("agents.research_agent._tool_web_search") as mock_web:
            mock_claude.messages.create = AsyncMock(side_effect=responses)
            mock_web.return_value = [{"title": "Nike Campaign", "snippet": "Nike launches...", "url": "http://example.com"}]

            from agents.research_agent import run_research_agent
            brief = await run_research_agent("Nike", meeting_title="Q2 call")

        assert "Nike" in brief
        mock_web.assert_called_once()

    @pytest.mark.asyncio
    async def test_attio_history_tool_called(self):
        """Research agent should call get_attio_deal_history and handle results."""
        attio_result = {
            "found": True,
            "deals": [{"name": "Nike Q2", "stage": "Negotiation", "probability": 80}],
        }

        responses = [
            _response("tool_use", [_tool_use_block("get_attio_deal_history", {"company_name": "Nike"})]),
            _response("end_turn", [_text_block("*Pre-Meeting Brief: Nike*\n\n*Deal Context*\n• Stage: Negotiation, 80%")]),
        ]

        with patch("agents.research_agent._claude") as mock_claude, \
             patch("agents.research_agent._tool_get_attio_history", new_callable=AsyncMock) as mock_attio:
            mock_claude.messages.create = AsyncMock(side_effect=responses)
            mock_attio.return_value = attio_result

            from agents.research_agent import run_research_agent
            brief = await run_research_agent("Nike")

        assert "Nike" in brief

    @pytest.mark.asyncio
    async def test_fallback_on_max_turns(self):
        """Should return a polite fallback when MAX_TURNS is exceeded."""
        with patch("agents.research_agent._claude") as mock_claude, \
             patch("agents.research_agent._tool_web_search") as mock_web:
            mock_claude.messages.create = AsyncMock(
                return_value=_response("tool_use", [_tool_use_block("web_search", {"query": "test"})])
            )
            mock_web.return_value = []

            from agents.research_agent import run_research_agent
            result = await run_research_agent("UnknownCo")

        assert "timed out" in result.lower() or "UnknownCo" in result

    @pytest.mark.asyncio
    async def test_empty_response_returns_fallback(self):
        """Empty text response should return the no-research-found message."""
        with patch("agents.research_agent._claude") as mock_claude:
            mock_claude.messages.create = AsyncMock(
                return_value=_response("end_turn", [_text_block("")])
            )
            from agents.research_agent import run_research_agent
            result = await run_research_agent("EmptyCo")

        assert "No research found" in result or "EmptyCo" in result


# ═══════════════════════════════════════════════════════════════════════════════
# Production Planner Agent
# ═══════════════════════════════════════════════════════════════════════════════

def _make_won_deal(name="Nike TVC", value=80000, rid="rid_nike_won"):
    """Minimal Attio won deal dict."""
    return {
        "id": {"record_id": rid},
        "values": {
            "name": [{"value": name}],
            "stage": [{"option": {"title": "Won"}}],
            "probability": [{"value": 100}],
            "value": [{"value": value}],
            "close_date": [{"value": "2025-07-31"}],
            "owner": [{"value": "Louis"}],
        },
        "updated_at": "2025-06-01T10:00:00Z",
    }


class TestProductionPlannerAgent:
    """Tests for agents.production_planner_agent.run_production_planner"""

    @pytest.mark.asyncio
    async def test_returns_brief_on_end_turn(self):
        """Should return Claude's text as the production brief."""
        deal = _make_won_deal()

        with patch("agents.production_planner_agent._claude") as mock_claude:
            mock_claude.messages.create = AsyncMock(
                return_value=_response("end_turn", [_text_block(
                    "*Production Plan: Nike TVC*\n\nWeek 1-2: Pre-production..."
                )])
            )
            from agents.production_planner_agent import run_production_planner
            brief = await run_production_planner(deal)

        assert "Production Plan" in brief or "Nike" in brief

    @pytest.mark.asyncio
    async def test_write_notion_tool_called(self):
        """Agent should call write_production_plan_to_notion tool."""
        deal = _make_won_deal()

        notion_result = {"success": True, "action": "created", "project": "Nike TVC", "duration_weeks": 6}

        responses = [
            _response("tool_use", [_tool_use_block("write_production_plan_to_notion", {
                "attio_record_id": "rid_nike_won",
                "project_name": "Nike TVC",
                "deliverable_type": "Commercial",
                "projected_start": "2025-07-01",
                "duration_weeks": 6,
                "crew_notes": "Week 1-2: Pre-prod\nWeek 3: Shoot\nWeek 4-6: Post",
            })]),
            _response("end_turn", [_text_block("Production plan created. Notion updated. :white_check_mark:")]),
        ]

        with patch("agents.production_planner_agent._claude") as mock_claude, \
             patch("agents.production_planner_agent._tool_write_notion_plan", new_callable=AsyncMock) as mock_notion:
            mock_claude.messages.create = AsyncMock(side_effect=responses)
            mock_notion.return_value = notion_result

            from agents.production_planner_agent import run_production_planner
            brief = await run_production_planner(deal)

        mock_notion.assert_called_once()
        assert "Production plan" in brief or "Notion" in brief

    @pytest.mark.asyncio
    async def test_capacity_check_tool_called(self):
        """Agent should optionally call get_pipeline_for_capacity_check."""
        deal = _make_won_deal()

        conflict_deals = [{"name": "Adidas Campaign", "probability": 75, "close_date": "2025-07-15"}]

        responses = [
            _response("tool_use", [_tool_use_block("get_pipeline_for_capacity_check", {
                "start_date": "2025-07-01",
                "end_date": "2025-08-15",
            })]),
            _response("tool_use", [_tool_use_block("write_production_plan_to_notion", {
                "attio_record_id": "rid_nike_won",
                "project_name": "Nike TVC",
                "deliverable_type": "Commercial",
                "projected_start": "2025-07-01",
                "duration_weeks": 6,
                "crew_notes": "Week 1-2: Pre-prod. NOTE: Capacity conflict with Adidas.",
            })]),
            _response("end_turn", [_text_block("Plan created. ⚠️ Capacity conflict with Adidas in July.")]),
        ]

        with patch("agents.production_planner_agent._claude") as mock_claude, \
             patch("agents.production_planner_agent._tool_capacity_check", new_callable=AsyncMock) as mock_cap, \
             patch("agents.production_planner_agent._tool_write_notion_plan", new_callable=AsyncMock) as mock_notion:
            mock_claude.messages.create = AsyncMock(side_effect=responses)
            mock_cap.return_value = conflict_deals
            mock_notion.return_value = {"success": True, "action": "created"}

            from agents.production_planner_agent import run_production_planner
            brief = await run_production_planner(deal)

        mock_cap.assert_called_once()
        assert brief  # Should return something

    @pytest.mark.asyncio
    async def test_max_turns_fallback(self):
        """Returns timeout message when MAX_TURNS is exhausted."""
        deal = _make_won_deal("UnknownProject")

        with patch("agents.production_planner_agent._claude") as mock_claude, \
             patch("agents.production_planner_agent._tool_capacity_check", new_callable=AsyncMock) as mock_cap:
            mock_claude.messages.create = AsyncMock(
                return_value=_response("tool_use", [_tool_use_block("get_pipeline_for_capacity_check", {
                    "start_date": "2025-07-01", "end_date": "2025-08-01",
                })])
            )
            mock_cap.return_value = []

            from agents.production_planner_agent import run_production_planner
            result = await run_production_planner(deal)

        assert "timed out" in result.lower() or "UnknownProject" in result


# ═══════════════════════════════════════════════════════════════════════════════
# Signal Agent
# ═══════════════════════════════════════════════════════════════════════════════

class TestSignalAgent:
    """Tests for agents.signal_agent.run_signal_agent"""

    _VALID_RESULT = {
        "deal_name": "Nike",
        "record_id": "rid_nike",
        "confidence": "high",
        "note_title": "Email signal — budget confirmed",
        "note_body": "Nike confirmed a $120k budget for the Q2 TVC production.",
        "key_signals": ["$120k budget", "Q2 TVC"],
        "action_items": ["Send production proposal by Friday"],
        "urgency": "high",
        "logged": False,
        "candidates": [],
    }

    @pytest.mark.asyncio
    async def test_high_confidence_json_returned(self):
        """Should parse JSON from Claude response and return structured dict."""
        with patch("agents.signal_agent._claude") as mock_claude:
            mock_claude.messages.create = AsyncMock(
                return_value=_response("end_turn", [_text_block(json.dumps(self._VALID_RESULT))])
            )
            from agents.signal_agent import run_signal_agent
            result = await run_signal_agent("Nike confirmed $120k for Q2 TVC", source="email")

        assert result["deal_name"] == "Nike"
        assert result["confidence"] == "high"
        assert result["logged"] is False

    @pytest.mark.asyncio
    async def test_search_deals_tool_called(self):
        """Agent should call search_deals to validate the deal match."""
        search_result = [{"record_id": "rid_nike", "name": "Nike Q2 TVC", "stage": "Negotiation", "probability": 80}]

        final_result = {**self._VALID_RESULT, "record_id": "rid_nike"}

        responses = [
            _response("tool_use", [_tool_use_block("search_deals", {"query": "Nike"})]),
            _response("end_turn", [_text_block(json.dumps(final_result))]),
        ]

        with patch("agents.signal_agent._claude") as mock_claude, \
             patch("agents.signal_agent._tool_search_deals", new_callable=AsyncMock) as mock_search:
            mock_claude.messages.create = AsyncMock(side_effect=responses)
            mock_search.return_value = search_result

            from agents.signal_agent import run_signal_agent
            result = await run_signal_agent("Budget confirmed for Nike project", source="email")

        mock_search.assert_called_once()
        assert result.get("record_id") == "rid_nike"

    @pytest.mark.asyncio
    async def test_auto_log_calls_log_tool(self):
        """When auto_log=True, agent should call log_signal_to_deal."""
        logged_result = {**self._VALID_RESULT, "logged": True}

        responses = [
            _response("tool_use", [_tool_use_block("log_signal_to_deal", {
                "record_id": "rid_nike",
                "title": "Email signal — budget confirmed",
                "body": "Nike confirmed $120k budget.",
            })]),
            _response("end_turn", [_text_block(json.dumps(logged_result))]),
        ]

        with patch("agents.signal_agent._claude") as mock_claude, \
             patch("agents.signal_agent._tool_log_signal", new_callable=AsyncMock) as mock_log:
            mock_claude.messages.create = AsyncMock(side_effect=responses)
            mock_log.return_value = {"success": True, "record_id": "rid_nike"}

            from agents.signal_agent import run_signal_agent
            result = await run_signal_agent(
                "Nike confirmed $120k budget for Q2 TVC.",
                source="email",
                auto_log=True,
            )

        mock_log.assert_called_once()
        assert result.get("logged") is True

    @pytest.mark.asyncio
    async def test_non_json_response_returns_empty(self):
        """If Claude returns non-JSON, return the empty fallback dict."""
        with patch("agents.signal_agent._claude") as mock_claude:
            mock_claude.messages.create = AsyncMock(
                return_value=_response("end_turn", [_text_block("I couldn't parse this email.")])
            )
            from agents.signal_agent import run_signal_agent
            result = await run_signal_agent("gibberish text", source="email")

        assert result["deal_name"] is None
        assert result["confidence"] == "low"
        assert result["logged"] is False

    @pytest.mark.asyncio
    async def test_json_in_markdown_fences_parsed(self):
        """JSON wrapped in markdown code fences should still be parsed."""
        fenced = f"```json\n{json.dumps(self._VALID_RESULT)}\n```"

        with patch("agents.signal_agent._claude") as mock_claude:
            mock_claude.messages.create = AsyncMock(
                return_value=_response("end_turn", [_text_block(fenced)])
            )
            from agents.signal_agent import run_signal_agent
            result = await run_signal_agent("test", source="email")

        assert result["deal_name"] == "Nike"

    @pytest.mark.asyncio
    async def test_missing_keys_filled_with_defaults(self):
        """Partial JSON result should have missing keys filled with safe defaults."""
        partial = {"deal_name": "Adidas", "confidence": "medium"}

        with patch("agents.signal_agent._claude") as mock_claude:
            mock_claude.messages.create = AsyncMock(
                return_value=_response("end_turn", [_text_block(json.dumps(partial))])
            )
            from agents.signal_agent import run_signal_agent
            result = await run_signal_agent("Adidas reached out about a campaign.", source="email")

        assert result["deal_name"] == "Adidas"
        assert result["logged"] is False      # default
        assert result["candidates"] == []     # default
        assert result["key_signals"] == []    # default

    @pytest.mark.asyncio
    async def test_max_turns_returns_empty(self):
        """MAX_TURNS exhaustion returns the empty fallback result."""
        with patch("agents.signal_agent._claude") as mock_claude, \
             patch("agents.signal_agent._tool_search_deals", new_callable=AsyncMock) as mock_search:
            mock_claude.messages.create = AsyncMock(
                return_value=_response("tool_use", [_tool_use_block("search_deals", {"query": "X"})])
            )
            mock_search.return_value = []

            from agents.signal_agent import run_signal_agent
            result = await run_signal_agent("test", source="email")

        assert result["deal_name"] is None
        assert result["confidence"] == "low"


# ═══════════════════════════════════════════════════════════════════════════════
# Tool-level unit tests (fast, no Claude mock needed)
# ═══════════════════════════════════════════════════════════════════════════════

class TestSignalAgentTools:
    """Direct unit tests for signal agent tool implementations."""

    @pytest.mark.asyncio
    async def test_search_deals_returns_matches(self):
        """_tool_search_deals should filter deals by name substring."""
        fake_deals = [
            {
                "id": {"record_id": "r1"},
                "values": {
                    "name": [{"value": "Nike Q2 TVC"}],
                    "stage": [{"option": {"title": "Proposal Sent"}}],
                    "probability": [{"value": 70}],
                    "value": [{"value": 50000}],
                    "close_date": [],
                    "owner": [{"value": "Louis"}],
                },
                "updated_at": "2025-05-01T00:00:00Z",
            },
            {
                "id": {"record_id": "r2"},
                "values": {
                    "name": [{"value": "Adidas Campaign"}],
                    "stage": [{"option": {"title": "Negotiation"}}],
                    "probability": [{"value": 85}],
                    "value": [{"value": 80000}],
                    "close_date": [],
                    "owner": [{"value": "Marie"}],
                },
                "updated_at": "2025-05-02T00:00:00Z",
            },
        ]

        with patch("agents.signal_agent.attio") as mock_attio:
            mock_attio.list_deals = AsyncMock(return_value=fake_deals)

            from agents.signal_agent import _tool_search_deals
            results = await _tool_search_deals("nike")

        assert len(results) == 1
        assert results[0]["name"] == "Nike Q2 TVC"
        assert results[0]["record_id"] == "r1"

    @pytest.mark.asyncio
    async def test_search_deals_empty_query_returns_all(self):
        """Empty query... wait, query is required but let's verify no match returns []."""
        with patch("agents.signal_agent.attio") as mock_attio:
            mock_attio.list_deals = AsyncMock(return_value=[])
            from agents.signal_agent import _tool_search_deals
            results = await _tool_search_deals("nonexistent brand xyz")
        assert results == []


class TestProductionPlannerTools:
    """Direct unit tests for production planner tool implementations."""

    @pytest.mark.asyncio
    async def test_capacity_check_finds_overlap(self):
        """_tool_capacity_check should return deals whose close date is in the window."""
        july_deal = {
            "id": {"record_id": "r1"},
            "values": {
                "name": [{"value": "Adidas July Campaign"}],
                "stage": [{"option": {"title": "Proposal Sent"}}],
                "probability": [{"value": 70}],
                "value": [{"value": 60000}],
                "close_date": [{"value": "2025-07-15"}],
                "owner": [{"value": "Louis"}],
            },
            "updated_at": "2025-05-01T00:00:00Z",
        }
        august_deal = {
            "id": {"record_id": "r2"},
            "values": {
                "name": [{"value": "Puma August Launch"}],
                "stage": [{"option": {"title": "Negotiation"}}],
                "probability": [{"value": 80}],
                "value": [{"value": 90000}],
                "close_date": [{"value": "2025-08-31"}],
                "owner": [{"value": "Marie"}],
            },
            "updated_at": "2025-05-02T00:00:00Z",
        }

        with patch("agents.production_planner_agent.attio") as mock_attio:
            mock_attio.get_active_deals = AsyncMock(return_value=[july_deal, august_deal])

            from agents.production_planner_agent import _tool_capacity_check
            # Window: July only
            conflicts = await _tool_capacity_check("2025-07-01", "2025-07-31")

        assert len(conflicts) == 1
        assert conflicts[0]["name"] == "Adidas July Campaign"


# ═══════════════════════════════════════════════════════════════════════════════
# _parse_notion_page helper
# ═══════════════════════════════════════════════════════════════════════════════

class TestParseNotionPage:
    """Unit tests for the _parse_notion_page helper in viktor_tool_agent."""

    def _page(self, **prop_overrides) -> dict:
        """Build a fully-populated fake Notion page dict."""
        props = {
            "Project Name": {"title": [{"text": {"content": "Nike TVC"}}]},
            "Client": {"rich_text": [{"text": {"content": "Nike"}}]},
            "Deliverable Type": {"select": {"name": "Commercial"}},
            "Production Status": {"select": {"name": "In Production"}},
            "Stage": {"select": {"name": "Won"}},
            "Close Date": {"date": {"start": "2025-07-31"}},
            "Projected Start": {"date": {"start": "2025-08-01"}},
            "Duration (weeks)": {"number": 6},
            "Deal Value": {"number": 80000},
            "Production Lead": {"rich_text": [{"text": {"content": "Marie"}}]},
            "Attio Record ID": {"rich_text": [{"text": {"content": "rid_nike"}}]},
        }
        props.update(prop_overrides)
        return {"properties": props}

    def test_full_page_parsed_correctly(self):
        from agents.viktor_tool_agent import _parse_notion_page
        result = _parse_notion_page(self._page())
        assert result["name"] == "Nike TVC"
        assert result["client"] == "Nike"
        assert result["deliverable_type"] == "Commercial"
        assert result["production_status"] == "In Production"
        assert result["stage"] == "Won"
        assert result["close_date"] == "2025-07-31"
        assert result["projected_start"] == "2025-08-01"
        assert result["duration_weeks"] == 6
        assert result["deal_value"] == 80000
        assert result["production_lead"] == "Marie"
        assert result["attio_record_id"] == "rid_nike"

    def test_missing_properties_return_empty_or_none(self):
        from agents.viktor_tool_agent import _parse_notion_page
        result = _parse_notion_page({"properties": {}})
        assert result["name"] == ""           # title with no items → empty string
        assert result["deliverable_type"] is None
        assert result["production_status"] is None
        assert result["close_date"] is None
        assert result["duration_weeks"] is None

    def test_null_select_returns_none(self):
        from agents.viktor_tool_agent import _parse_notion_page
        page = self._page(**{"Deliverable Type": {"select": None}})
        assert _parse_notion_page(page)["deliverable_type"] is None

    def test_null_date_returns_none(self):
        from agents.viktor_tool_agent import _parse_notion_page
        page = self._page(**{"Close Date": {"date": None}})
        assert _parse_notion_page(page)["close_date"] is None

    def test_multi_segment_rich_text_concatenated(self):
        from agents.viktor_tool_agent import _parse_notion_page
        page = self._page(**{
            "Client": {"rich_text": [
                {"text": {"content": "Big "}},
                {"text": {"content": "Client"}},
            ]},
        })
        assert _parse_notion_page(page)["client"] == "Big Client"


# ═══════════════════════════════════════════════════════════════════════════════
# _tool_search_notion
# ═══════════════════════════════════════════════════════════════════════════════

class TestToolSearchNotion:
    """Tests for _tool_search_notion (Notion Production Calendar lookup)."""

    def _make_page(self, name: str, status: str) -> dict:
        return {
            "properties": {
                "Project Name": {"title": [{"text": {"content": name}}]},
                "Client": {"rich_text": []},
                "Deliverable Type": {"select": None},
                "Production Status": {"select": {"name": status}},
                "Stage": {"select": None},
                "Close Date": {"date": None},
                "Projected Start": {"date": None},
                "Duration (weeks)": {"number": None},
                "Deal Value": {"number": None},
                "Production Lead": {"rich_text": []},
                "Attio Record ID": {"rich_text": []},
            }
        }

    @pytest.mark.asyncio
    async def test_no_filter_returns_all_pages(self):
        pages = [
            self._make_page("Nike TVC", "In Production"),
            self._make_page("Adidas Campaign", "Post"),
        ]
        with patch("agents.viktor_tool_agent.notion_db") as mock_notion:
            mock_notion.get_all_pages = AsyncMock(return_value=pages)
            from agents.viktor_tool_agent import _tool_search_notion
            results = await _tool_search_notion()
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_name_filter_case_insensitive(self):
        pages = [
            self._make_page("Nike TVC", "In Production"),
            self._make_page("Adidas Campaign", "Post"),
        ]
        with patch("agents.viktor_tool_agent.notion_db") as mock_notion:
            mock_notion.get_all_pages = AsyncMock(return_value=pages)
            from agents.viktor_tool_agent import _tool_search_notion
            results = await _tool_search_notion(name_query="nike")
        assert len(results) == 1
        assert results[0]["name"] == "Nike TVC"

    @pytest.mark.asyncio
    async def test_status_filter_case_insensitive(self):
        pages = [
            self._make_page("Nike TVC", "In Production"),
            self._make_page("Adidas Campaign", "Post"),
        ]
        with patch("agents.viktor_tool_agent.notion_db") as mock_notion:
            mock_notion.get_all_pages = AsyncMock(return_value=pages)
            from agents.viktor_tool_agent import _tool_search_notion
            results = await _tool_search_notion(status_filter="post")
        assert len(results) == 1
        assert results[0]["name"] == "Adidas Campaign"

    @pytest.mark.asyncio
    async def test_no_match_returns_empty_list(self):
        pages = [self._make_page("Nike TVC", "In Production")]
        with patch("agents.viktor_tool_agent.notion_db") as mock_notion:
            mock_notion.get_all_pages = AsyncMock(return_value=pages)
            from agents.viktor_tool_agent import _tool_search_notion
            results = await _tool_search_notion(name_query="puma")
        assert results == []

    @pytest.mark.asyncio
    async def test_capped_at_15_results(self):
        """Should never return more than 15 results regardless of input size."""
        pages = [self._make_page(f"Project {i}", "In Production") for i in range(20)]
        with patch("agents.viktor_tool_agent.notion_db") as mock_notion:
            mock_notion.get_all_pages = AsyncMock(return_value=pages)
            from agents.viktor_tool_agent import _tool_search_notion
            results = await _tool_search_notion()
        assert len(results) == 15

    @pytest.mark.asyncio
    async def test_name_and_status_filters_combined(self):
        pages = [
            self._make_page("Nike TVC", "In Production"),
            self._make_page("Nike Reel", "Post"),
            self._make_page("Adidas Campaign", "In Production"),
        ]
        with patch("agents.viktor_tool_agent.notion_db") as mock_notion:
            mock_notion.get_all_pages = AsyncMock(return_value=pages)
            from agents.viktor_tool_agent import _tool_search_notion
            results = await _tool_search_notion(name_query="nike", status_filter="in production")
        assert len(results) == 1
        assert results[0]["name"] == "Nike TVC"


# ═══════════════════════════════════════════════════════════════════════════════
# _tool_get_meetings
# ═══════════════════════════════════════════════════════════════════════════════

class TestToolGetMeetings:
    """Tests for _tool_get_meetings (Google Calendar)."""

    def _fake_meeting(self, **overrides) -> dict:
        m = {
            "title": "Nike Scope Call",
            "start": datetime(2025, 7, 1, 14, 0, tzinfo=timezone.utc),
            "minutes_until_start": 120,
            "external_attendees": ["john@nike.com"],
            "organizer": "Louis",
            "meet_link": "https://meet.google.com/abc-def",
        }
        m.update(overrides)
        return m

    @pytest.mark.asyncio
    async def test_returns_formatted_meeting(self):
        with patch("agents.viktor_tool_agent.gcal") as mock_gcal:
            mock_gcal.get_upcoming_prospect_meetings.return_value = [self._fake_meeting()]
            from agents.viktor_tool_agent import _tool_get_meetings
            results = await _tool_get_meetings(hours_ahead=48)

        assert len(results) == 1
        r = results[0]
        assert r["title"] == "Nike Scope Call"
        assert r["external_attendees"] == ["john@nike.com"]
        assert r["organizer"] == "Louis"
        assert r["meet_link"] == "https://meet.google.com/abc-def"
        assert r["minutes_until_start"] == 120
        assert "2025" in r["start"] or "Jul" in r["start"]  # formatted date string

    @pytest.mark.asyncio
    async def test_empty_calendar_returns_empty_list(self):
        with patch("agents.viktor_tool_agent.gcal") as mock_gcal:
            mock_gcal.get_upcoming_prospect_meetings.return_value = []
            from agents.viktor_tool_agent import _tool_get_meetings
            results = await _tool_get_meetings()
        assert results == []

    @pytest.mark.asyncio
    async def test_gcal_exception_returns_error_entry(self):
        with patch("agents.viktor_tool_agent.gcal") as mock_gcal:
            mock_gcal.get_upcoming_prospect_meetings.side_effect = Exception("Auth failed")
            from agents.viktor_tool_agent import _tool_get_meetings
            results = await _tool_get_meetings()
        assert len(results) == 1
        assert "error" in results[0]
        assert "Auth failed" in results[0]["error"]

    @pytest.mark.asyncio
    async def test_hours_ahead_passed_to_client(self):
        with patch("agents.viktor_tool_agent.gcal") as mock_gcal:
            mock_gcal.get_upcoming_prospect_meetings.return_value = []
            from agents.viktor_tool_agent import _tool_get_meetings
            await _tool_get_meetings(hours_ahead=24)
        mock_gcal.get_upcoming_prospect_meetings.assert_called_once_with(hours_ahead=24)

    @pytest.mark.asyncio
    async def test_multiple_meetings_all_returned(self):
        meetings = [
            self._fake_meeting(title="Call A"),
            self._fake_meeting(title="Call B"),
            self._fake_meeting(title="Call C"),
        ]
        with patch("agents.viktor_tool_agent.gcal") as mock_gcal:
            mock_gcal.get_upcoming_prospect_meetings.return_value = meetings
            from agents.viktor_tool_agent import _tool_get_meetings
            results = await _tool_get_meetings()
        assert len(results) == 3
        assert [r["title"] for r in results] == ["Call A", "Call B", "Call C"]


# ═══════════════════════════════════════════════════════════════════════════════
# _tool_get_email_signals
# ═══════════════════════════════════════════════════════════════════════════════

class TestToolGetEmailSignals:
    """Tests for _tool_get_email_signals (Gmail deal-signal scanner)."""

    def _fake_signal(self, **overrides) -> dict:
        s = {
            "sender": "client@nike.com",
            "subject": "Re: Q2 TVC proposal",
            "date": datetime(2025, 7, 1, 9, 0, tzinfo=timezone.utc),
            "snippet": "We are happy to confirm the $120k budget for the Q2 TVC project.",
            "matched_keywords": ["budget", "confirm"],
        }
        s.update(overrides)
        return s

    @pytest.mark.asyncio
    async def test_returns_formatted_signal(self):
        with patch("agents.viktor_tool_agent.gmail") as mock_gmail:
            mock_gmail.scan_for_deal_signals.return_value = [self._fake_signal()]
            from agents.viktor_tool_agent import _tool_get_email_signals
            results = await _tool_get_email_signals(hours_back=48)

        assert len(results) == 1
        r = results[0]
        assert r["sender"] == "client@nike.com"
        assert r["subject"] == "Re: Q2 TVC proposal"
        assert "budget" in r["matched_keywords"]
        assert r["snippet"]  # non-empty

    @pytest.mark.asyncio
    async def test_snippet_capped_at_300_chars(self):
        long_signal = self._fake_signal(snippet="x" * 500)
        with patch("agents.viktor_tool_agent.gmail") as mock_gmail:
            mock_gmail.scan_for_deal_signals.return_value = [long_signal]
            from agents.viktor_tool_agent import _tool_get_email_signals
            results = await _tool_get_email_signals()
        assert len(results[0]["snippet"]) == 300

    @pytest.mark.asyncio
    async def test_gmail_exception_returns_error_entry(self):
        with patch("agents.viktor_tool_agent.gmail") as mock_gmail:
            mock_gmail.scan_for_deal_signals.side_effect = Exception("Token expired")
            from agents.viktor_tool_agent import _tool_get_email_signals
            results = await _tool_get_email_signals()
        assert len(results) == 1
        assert "error" in results[0]
        assert "Token expired" in results[0]["error"]

    @pytest.mark.asyncio
    async def test_max_results_capped_at_20(self):
        """Passing max_results > 20 should still call the client with ≤ 20."""
        with patch("agents.viktor_tool_agent.gmail") as mock_gmail:
            mock_gmail.scan_for_deal_signals.return_value = []
            from agents.viktor_tool_agent import _tool_get_email_signals
            await _tool_get_email_signals(hours_back=24, max_results=999)
        called_with = mock_gmail.scan_for_deal_signals.call_args.kwargs
        assert called_with.get("max_results", 0) <= 20

    @pytest.mark.asyncio
    async def test_empty_inbox_returns_empty_list(self):
        with patch("agents.viktor_tool_agent.gmail") as mock_gmail:
            mock_gmail.scan_for_deal_signals.return_value = []
            from agents.viktor_tool_agent import _tool_get_email_signals
            results = await _tool_get_email_signals()
        assert results == []

    @pytest.mark.asyncio
    async def test_date_formatted_as_string(self):
        """date field should be a formatted string, not a datetime object."""
        with patch("agents.viktor_tool_agent.gmail") as mock_gmail:
            mock_gmail.scan_for_deal_signals.return_value = [self._fake_signal()]
            from agents.viktor_tool_agent import _tool_get_email_signals
            results = await _tool_get_email_signals()
        assert isinstance(results[0]["date"], str)
        assert results[0]["date"]  # non-empty


# ═══════════════════════════════════════════════════════════════════════════════
# _react helper (main.py)
# ═══════════════════════════════════════════════════════════════════════════════

class TestMainReactHelper:
    """Tests for the _react emoji-reaction helper in main.py."""

    @pytest.mark.asyncio
    async def test_adds_reaction(self):
        from main import _react
        client = AsyncMock()
        await _react(client, "C123", "1234.5678", add="eyes")

        client.reactions_add.assert_called_once_with(
            channel="C123", timestamp="1234.5678", name="eyes"
        )
        client.reactions_remove.assert_not_called()

    @pytest.mark.asyncio
    async def test_removes_before_adding(self):
        from main import _react
        client = AsyncMock()
        await _react(client, "C123", "1234.5678", add="white_check_mark", remove="hourglass_flowing_sand")

        client.reactions_remove.assert_called_once_with(
            channel="C123", timestamp="1234.5678", name="hourglass_flowing_sand"
        )
        client.reactions_add.assert_called_once_with(
            channel="C123", timestamp="1234.5678", name="white_check_mark"
        )

    @pytest.mark.asyncio
    async def test_add_failure_silently_swallowed(self):
        """reactions_add raising should not propagate out of _react."""
        from main import _react
        client = AsyncMock()
        client.reactions_add = AsyncMock(side_effect=Exception("missing_scope"))
        # Must not raise
        await _react(client, "C123", "1234.5678", add="eyes")

    @pytest.mark.asyncio
    async def test_remove_failure_still_calls_add(self):
        """Even if reactions_remove raises, reactions_add should still be attempted."""
        from main import _react
        client = AsyncMock()
        client.reactions_remove = AsyncMock(side_effect=Exception("no_reaction"))
        await _react(client, "C123", "1234.5678", add="white_check_mark", remove="eyes")
        # Add should still be called after the failed remove
        client.reactions_add.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_remove_when_remove_is_none(self):
        from main import _react
        client = AsyncMock()
        await _react(client, "C123", "1234.5678", add="eyes", remove=None)
        client.reactions_remove.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# New tools via run_viktor (integration)
# ═══════════════════════════════════════════════════════════════════════════════

class TestViktorNewToolsIntegration:
    """Integration tests: run_viktor dispatches Notion / GCal / Gmail tools."""

    @pytest.mark.asyncio
    async def test_search_notion_dispatched(self):
        """run_viktor should call _tool_search_notion when Claude requests it."""
        say = AsyncMock()
        notion_result = [{"name": "Nike TVC", "production_status": "In Production"}]

        responses = [
            _response("tool_use", [_tool_use_block(
                "search_notion_production_calendar", {"name_query": "Nike"}
            )]),
            _response("end_turn", [_text_block("Nike TVC is currently In Production.")]),
        ]

        with patch("agents.viktor_tool_agent._claude") as mock_claude, \
             patch("agents.viktor_tool_agent._tool_search_notion", new_callable=AsyncMock) as mock_notion:
            mock_claude.messages.create = AsyncMock(side_effect=responses)
            mock_notion.return_value = notion_result
            from agents.viktor_tool_agent import run_viktor
            await run_viktor("what's in production right now?", say)

        mock_notion.assert_called_once_with(name_query="Nike")
        say.assert_called_once()
        assert "Nike" in say.call_args.kwargs.get("text", "")

    @pytest.mark.asyncio
    async def test_get_meetings_dispatched(self):
        """run_viktor should call _tool_get_meetings when Claude requests it."""
        say = AsyncMock()
        meetings_result = [{"title": "Nike Scope Call", "start": "Tuesday Jul 01, 02:00 PM"}]

        responses = [
            _response("tool_use", [_tool_use_block("get_upcoming_meetings", {"hours_ahead": 24})]),
            _response("end_turn", [_text_block("You have a Nike call tomorrow at 2 pm.")]),
        ]

        with patch("agents.viktor_tool_agent._claude") as mock_claude, \
             patch("agents.viktor_tool_agent._tool_get_meetings", new_callable=AsyncMock) as mock_mtg:
            mock_claude.messages.create = AsyncMock(side_effect=responses)
            mock_mtg.return_value = meetings_result
            from agents.viktor_tool_agent import run_viktor
            await run_viktor("what meetings do I have tomorrow?", say)

        mock_mtg.assert_called_once_with(hours_ahead=24)
        say.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_email_signals_dispatched(self):
        """run_viktor should call _tool_get_email_signals when Claude requests it."""
        say = AsyncMock()
        emails_result = [{"sender": "ceo@nike.com", "subject": "Budget confirmed"}]

        responses = [
            _response("tool_use", [_tool_use_block("get_recent_email_signals", {"hours_back": 72})]),
            _response("end_turn", [_text_block("Nike's CEO confirmed the budget by email.")]),
        ]

        with patch("agents.viktor_tool_agent._claude") as mock_claude, \
             patch("agents.viktor_tool_agent._tool_get_email_signals", new_callable=AsyncMock) as mock_email:
            mock_claude.messages.create = AsyncMock(side_effect=responses)
            mock_email.return_value = emails_result
            from agents.viktor_tool_agent import run_viktor
            await run_viktor("any emails from clients this week?", say)

        mock_email.assert_called_once_with(hours_back=72)
        say.assert_called_once()

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error_and_continues(self):
        """Unknown tool names should return an error dict without crashing the loop."""
        say = AsyncMock()

        responses = [
            _response("tool_use", [_tool_use_block("nonexistent_tool", {})]),
            _response("end_turn", [_text_block("Something happened.")]),
        ]

        with patch("agents.viktor_tool_agent._claude") as mock_claude:
            mock_claude.messages.create = AsyncMock(side_effect=responses)
            from agents.viktor_tool_agent import run_viktor
            await run_viktor("do the impossible", say)

        # Should have posted a response anyway
        say.assert_called_once()
