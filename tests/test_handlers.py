"""
Unit tests for automation handlers.
All Attio/Slack/Claude calls are mocked.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone
from freezegun import freeze_time


# ── A1: Quick Update ───────────────────────────────────────────────────────────

class TestA1QuickUpdate:
    def _make_deal(self, name="Nike", record_id="abc123"):
        return {
            "id": {"record_id": record_id},
            "values": {
                "name": [{"value": name}],
                "stage": [{"option": {"title": "Negotiation"}}],
                "probability": [{"value": 60.0}],
                "value": [{"value": 50000.0}],
                "close_date": [{"value": "2025-05-31"}],
                "owner": [{"value": "Louis"}],
            },
        }

    @pytest.mark.asyncio
    async def test_update_probability_success(self):
        intent = {
            "intent": "update_deal",
            "deal_name": "Nike",
            "field": "probability",
            "new_value": "85",
            "filters": {},
        }
        deal = self._make_deal("Nike")
        say = AsyncMock()

        with (
            patch("handlers.a1_quick_update.parse_intent", return_value=intent),
            patch("handlers.a1_quick_update.attio.find_deal_by_name", return_value=deal),
            patch("handlers.a1_quick_update.attio.update_deal", return_value={}) as mock_update,
        ):
            from handlers.a1_quick_update import handle_quick_update
            await handle_quick_update("@Viktor update Nike to 85%", say, MagicMock())

        mock_update.assert_called_once_with("abc123", {"probability": 85.0})
        say.assert_called_once()
        assert "Nike" in say.call_args[0][0]
        assert "85%" in say.call_args[0][0]

    @pytest.mark.asyncio
    async def test_deal_not_found_replies_gracefully(self):
        intent = {
            "intent": "update_deal",
            "deal_name": "Unknown Brand",
            "field": "probability",
            "new_value": "50",
        }
        say = AsyncMock()

        with (
            patch("handlers.a1_quick_update.parse_intent", return_value=intent),
            patch("handlers.a1_quick_update.attio.find_deal_by_name", return_value=None),
        ):
            from handlers.a1_quick_update import handle_quick_update
            await handle_quick_update("@Viktor update Unknown Brand to 50%", say, MagicMock())

        say.assert_called_once()
        assert "couldn't find" in say.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_non_update_intent_gives_help(self):
        intent = {"intent": "query_pipeline"}
        say = AsyncMock()

        with patch("handlers.a1_quick_update.parse_intent", return_value=intent):
            from handlers.a1_quick_update import handle_quick_update
            await handle_quick_update("what are our deals?", say, MagicMock())

        say.assert_called_once()
        assert "update" in say.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_update_stage(self):
        intent = {
            "intent": "update_deal",
            "deal_name": "Nike",
            "field": "stage",
            "new_value": "Proposal Sent",
        }
        deal = self._make_deal("Nike")
        say = AsyncMock()

        with (
            patch("handlers.a1_quick_update.parse_intent", return_value=intent),
            patch("handlers.a1_quick_update.attio.find_deal_by_name", return_value=deal),
            patch("handlers.a1_quick_update.attio.update_deal", return_value={}) as mock_update,
        ):
            from handlers.a1_quick_update import handle_quick_update
            await handle_quick_update("@Viktor move Nike to Proposal Sent", say, MagicMock())

        mock_update.assert_called_once_with("abc123", {"stage": "Proposal Sent"})


# ── B1: Monday Forecast ────────────────────────────────────────────────────────

class TestB1MondayForecast:
    @pytest.mark.asyncio
    async def test_posts_to_deal_radar(self):
        deals = [
            {
                "id": {"record_id": "d1"},
                "values": {
                    "name": [{"value": "Nike"}],
                    "stage": [{"option": {"title": "Negotiation"}}],
                    "probability": [{"value": 80.0}],
                    "value": [{"value": 100000.0}],
                    "close_date": [{"value": "2025-04-30"}],
                    "owner": [{"value": "Louis"}],
                },
            }
        ]
        slack_client = AsyncMock()
        slack_client.chat_postMessage = AsyncMock()

        with (
            patch("handlers.b1_monday_forecast.attio.list_deals", return_value=deals),
            patch(
                "handlers.b1_monday_forecast.generate_monday_forecast_narrative",
                return_value="Pipeline looking strong this week.",
            ),
        ):
            from handlers.b1_monday_forecast import post_monday_forecast
            await post_monday_forecast(slack_client)

        slack_client.chat_postMessage.assert_called_once()
        call_kwargs = slack_client.chat_postMessage.call_args.kwargs
        assert "blocks" in call_kwargs

    @pytest.mark.asyncio
    async def test_empty_pipeline_still_posts(self):
        slack_client = AsyncMock()
        slack_client.chat_postMessage = AsyncMock()

        with (
            patch("handlers.b1_monday_forecast.attio.list_deals", return_value=[]),
            patch(
                "handlers.b1_monday_forecast.generate_monday_forecast_narrative",
                return_value="No active deals.",
            ),
        ):
            from handlers.b1_monday_forecast import post_monday_forecast
            await post_monday_forecast(slack_client)

        slack_client.chat_postMessage.assert_called_once()


# ── B2: Deal Movement ─────────────────────────────────────────────────────────

class TestB2DealMovement:
    def _deal(self, record_id, name, stage, prob, close_date=None):
        return {
            "id": {"record_id": record_id},
            "updated_at": "2025-02-01T10:00:00Z",
            "values": {
                "name": [{"value": name}],
                "stage": [{"option": {"title": stage}}],
                "probability": [{"value": prob}],
                "value": [{"value": 50000.0}],
                "close_date": [{"value": close_date or "2025-06-01"}],
                "owner": [{"value": "Louis"}],
            },
        }

    def _fresh_state(self):
        """Return a fresh temp-file StateStore for B2 isolation.
        Uses a real file (not :memory:) so connections share the same DB tables."""
        import tempfile
        from pathlib import Path
        from clients.state import StateStore
        _, path = tempfile.mkstemp(suffix=".db", prefix="test_b2_")
        return StateStore(db_path=Path(path))

    @pytest.mark.asyncio
    async def test_stage_change_triggers_alert(self):
        deal_v1 = self._deal("d1", "Nike", "Qualified", 50.0)
        deal_v2 = self._deal("d1", "Nike", "Negotiation", 50.0)
        fresh = self._fresh_state()
        slack_client = AsyncMock()
        slack_client.chat_postMessage = AsyncMock()

        with (
            patch("handlers.b2_deal_movement.state", fresh),
            patch("handlers.b2_deal_movement.attio.list_deals", return_value=[deal_v1]),
        ):
            import handlers.b2_deal_movement as b2
            await b2.seed_snapshot()

        with (
            patch("handlers.b2_deal_movement.state", fresh),
            patch("handlers.b2_deal_movement.attio.list_deals", return_value=[deal_v2]),
        ):
            await b2.run_deal_movement_check(slack_client)

        slack_client.chat_postMessage.assert_called_once()
        text = slack_client.chat_postMessage.call_args.kwargs["text"]
        assert "Negotiation" in text

    @pytest.mark.asyncio
    async def test_large_prob_shift_triggers_alert(self):
        deal_v1 = self._deal("d2", "Adidas", "Proposal Sent", 30.0)
        deal_v2 = self._deal("d2", "Adidas", "Proposal Sent", 70.0)
        fresh = self._fresh_state()
        slack_client = AsyncMock()
        slack_client.chat_postMessage = AsyncMock()

        with (
            patch("handlers.b2_deal_movement.state", fresh),
            patch("handlers.b2_deal_movement.attio.list_deals", return_value=[deal_v1]),
        ):
            import handlers.b2_deal_movement as b2
            await b2.seed_snapshot()

        with (
            patch("handlers.b2_deal_movement.state", fresh),
            patch("handlers.b2_deal_movement.attio.list_deals", return_value=[deal_v2]),
        ):
            await b2.run_deal_movement_check(slack_client)

        slack_client.chat_postMessage.assert_called_once()
        text = slack_client.chat_postMessage.call_args.kwargs["text"]
        assert "40 pts" in text or "40pts" in text or "probability" in text.lower()

    @pytest.mark.asyncio
    async def test_no_change_no_alert(self):
        deal = self._deal("d3", "Puma", "Lead", 20.0)
        fresh = self._fresh_state()
        slack_client = AsyncMock()
        slack_client.chat_postMessage = AsyncMock()

        with (
            patch("handlers.b2_deal_movement.state", fresh),
            patch("handlers.b2_deal_movement.attio.list_deals", return_value=[deal]),
        ):
            import handlers.b2_deal_movement as b2
            await b2.seed_snapshot()

        with (
            patch("handlers.b2_deal_movement.state", fresh),
            patch("handlers.b2_deal_movement.attio.list_deals", return_value=[deal]),
        ):
            await b2.run_deal_movement_check(slack_client)

        slack_client.chat_postMessage.assert_not_called()


# ── B5: Capacity Warnings ─────────────────────────────────────────────────────

class TestB5CapacityWarning:
    @freeze_time("2025-06-15")  # Freeze time → next month is always 2025-07
    @pytest.mark.asyncio
    async def test_conflict_detected_and_posted(self):
        # Three high-prob deals all closing in July 2025 (next month from frozen date)
        close_str = "2025-07-15"

        def make_deal(name):
            return {
                "id": {"record_id": name},
                "updated_at": "2025-06-01T00:00:00Z",
                "values": {
                    "name": [{"value": name}],
                    "stage": [{"option": {"title": "Negotiation"}}],
                    "probability": [{"value": 75.0}],
                    "value": [{"value": 80000.0}],
                    "close_date": [{"value": close_str}],
                    "owner": [{"value": "Louis"}],
                },
            }

        deals = [make_deal("Nike"), make_deal("Adidas"), make_deal("Puma")]
        slack_client = AsyncMock()
        slack_client.chat_postMessage = AsyncMock()

        with patch("handlers.b5_capacity_warning.attio.get_active_deals", return_value=deals):
            from handlers.b5_capacity_warning import run_capacity_check
            await run_capacity_check(slack_client)

        slack_client.chat_postMessage.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_conflict_no_post(self):
        slack_client = AsyncMock()
        slack_client.chat_postMessage = AsyncMock()

        with patch("handlers.b5_capacity_warning.attio.get_active_deals", return_value=[]):
            from handlers.b5_capacity_warning import run_capacity_check
            await run_capacity_check(slack_client)

        slack_client.chat_postMessage.assert_not_called()
