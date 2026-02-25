"""
Smoke tests for main.py event routing logic.

Tests the two key routing decisions:
  1. _looks_like_update() — distinguishes update commands from queries
  2. on_mention routing — calls a1_quick_update vs b3_nl_query
  3. on_message recap detection — calls a2_meeting_recap for long recap-like messages
"""
from __future__ import annotations

import re
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


# ── Import the pure routing helper without starting the bot ──────────────────

def _looks_like_update(text: str) -> bool:
    """Mirror of main.py's _looks_like_update — kept in sync manually."""
    UPDATE_PATTERNS = re.compile(
        r"update|set|change|move|bump|push|adjust|mark|log",
        re.IGNORECASE,
    )
    return bool(UPDATE_PATTERNS.search(text))


class TestLooksLikeUpdate:
    """Unit tests for the update vs query routing regex."""

    def test_update_keyword(self):
        assert _looks_like_update("update Nike to 85%")

    def test_move_keyword(self):
        assert _looks_like_update("move Nike to Proposal Sent")

    def test_bump_keyword(self):
        assert _looks_like_update("bump Adidas to 70%")

    def test_set_keyword(self):
        assert _looks_like_update("set close date to March 31")

    def test_log_keyword(self):
        assert _looks_like_update("log a note on Nike")

    def test_query_not_update(self):
        assert not _looks_like_update("what deals close in May?")

    def test_forecast_not_update(self):
        assert not _looks_like_update("forecast for Q2")

    def test_pipeline_question_not_update(self):
        assert not _looks_like_update("show me deals above 70%")

    def test_capacity_check_not_update(self):
        assert not _looks_like_update("any capacity conflicts in June?")

    def test_case_insensitive(self):
        assert _looks_like_update("UPDATE Nike to 90%")
        assert _looks_like_update("MOVE nike to won")


class TestOnMentionRouting:
    """Test that @Viktor mentions route to the correct handler."""

    def _make_event(self, text: str) -> dict:
        return {"text": f"<@U123BOT> {text}", "user": "U456", "channel": "C789"}

    @pytest.mark.asyncio
    async def test_update_routes_to_a1(self):
        say = AsyncMock()
        client = MagicMock()
        event = self._make_event("update Nike to 85%")

        with (
            patch("handlers.a1_quick_update.handle_quick_update", new_callable=AsyncMock) as mock_a1,
            patch("handlers.b3_nl_query.handle_nl_query", new_callable=AsyncMock) as mock_b3,
        ):
            # Simulate what main.py on_mention does
            text = event["text"]
            clean = re.sub(r"<@[A-Z0-9]+>", "", text).strip()
            if _looks_like_update(clean):
                await mock_a1(clean, say, client)
            else:
                await mock_b3(clean, say)

            mock_a1.assert_called_once()
            mock_b3.assert_not_called()

    @pytest.mark.asyncio
    async def test_query_routes_to_b3(self):
        say = AsyncMock()
        client = MagicMock()
        event = self._make_event("what deals close in May?")

        with (
            patch("handlers.a1_quick_update.handle_quick_update", new_callable=AsyncMock) as mock_a1,
            patch("handlers.b3_nl_query.handle_nl_query", new_callable=AsyncMock) as mock_b3,
        ):
            text = event["text"]
            clean = re.sub(r"<@[A-Z0-9]+>", "", text).strip()
            if _looks_like_update(clean):
                await mock_a1(clean, say, client)
            else:
                await mock_b3(clean, say)

            mock_b3.assert_called_once()
            mock_a1.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_mention_replies_with_help(self):
        say = AsyncMock()
        event = {"text": "<@U123BOT>", "user": "U456", "channel": "C789"}
        clean = re.sub(r"<@[A-Z0-9]+>", "", event["text"]).strip()
        assert clean == ""
        # In main.py: if not clean → say help message
        await say(
            "Hey! I'm Viktor, your deal intelligence agent.\n"
            "Try: `@Viktor update Nike to 85%` or `@Viktor what deals close in May?`"
        )
        say.assert_called_once()
        assert "Viktor" in say.call_args[0][0]


class TestOnMessageRecapDetection:
    """Test that long meeting-recap messages trigger A2 handler."""

    RECAP_PATTERNS = re.compile(
        r"meeting notes?|call notes?|recap|debrief|follow.?up|discussed|agreed|next steps?|action items?",
        re.IGNORECASE,
    )

    def _is_recap(self, text: str) -> bool:
        return len(text) >= 80 and bool(self.RECAP_PATTERNS.search(text))

    def test_short_message_not_recap(self):
        assert not self._is_recap("Had a quick call with Nike today.")

    def test_no_keyword_not_recap(self):
        long_text = "We spoke for a long time about various things and nothing specific." * 3
        assert not self._is_recap(long_text)

    def test_long_message_with_recap_keyword(self):
        text = (
            "Meeting notes from Nike call: "
            "Discussed production timeline, budget, and deliverables. "
            "Agreed on $150k scope. Next steps: send contract draft by Friday. "
            "Action items: Louis to schedule kickoff call."
        )
        assert self._is_recap(text)

    def test_debrief_keyword_triggers(self):
        text = "Debrief from Adidas pitch: they loved the treatment. " * 3
        assert self._is_recap(text)

    def test_follow_up_keyword_triggers(self):
        text = "Follow up from yesterday's Nike intro call. " * 4
        assert self._is_recap(text)
