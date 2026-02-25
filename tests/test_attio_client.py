"""Unit tests for the Attio client — attribute parsing and deal helpers."""
import pytest
from datetime import datetime, timezone
from clients.attio import AttioClient


def _make_deal(
    name="Nike",
    stage="Negotiation",
    probability=75.0,
    value=50000.0,
    close_date="2025-05-31",
    owner="Louis",
    updated_at="2025-02-01T12:00:00Z",
) -> dict:
    """Construct a minimal Attio deal record for testing."""
    return {
        "id": {"record_id": "test-record-123"},
        "updated_at": updated_at,
        "values": {
            "name": [{"value": name}],
            "stage": [{"option": {"title": stage}}],
            "probability": [{"value": probability}],
            "value": [{"value": value}],
            "close_date": [{"value": close_date}],
            "owner": [{"value": owner}],
        },
    }


class TestAttioAttributeReaders:
    def test_deal_name(self):
        assert AttioClient._deal_name(_make_deal(name="Nike")) == "Nike"

    def test_deal_name_missing(self):
        deal = _make_deal()
        deal["values"]["name"] = []
        assert AttioClient._deal_name(deal) == ""

    def test_deal_probability(self):
        assert AttioClient._deal_probability(_make_deal(probability=85.0)) == 85.0

    def test_deal_probability_none(self):
        deal = _make_deal()
        deal["values"]["probability"] = []
        assert AttioClient._deal_probability(deal) is None

    def test_deal_probability_zero_not_dropped(self):
        """Regression: probability=0 must NOT be treated as missing (falsy or-chain bug)."""
        deal = _make_deal(probability=0.0)
        assert AttioClient._deal_probability(deal) == 0.0

    def test_deal_value_zero_not_dropped(self):
        """Regression: value=0 must NOT be treated as missing."""
        deal = _make_deal(value=0.0)
        assert AttioClient._deal_value(deal) == 0.0

    def test_deal_stage(self):
        assert AttioClient._deal_stage(_make_deal(stage="Proposal Sent")) == "Proposal Sent"

    def test_deal_value(self):
        assert AttioClient._deal_value(_make_deal(value=120000.0)) == 120000.0

    def test_deal_close_date_parses(self):
        close = AttioClient._deal_close_date(_make_deal(close_date="2025-05-31"))
        assert close is not None
        assert close.year == 2025
        assert close.month == 5
        assert close.day == 31

    def test_deal_close_date_none_when_missing(self):
        deal = _make_deal()
        deal["values"]["close_date"] = []
        assert AttioClient._deal_close_date(deal) is None

    def test_deal_last_updated(self):
        deal = _make_deal(updated_at="2025-01-15T10:30:00Z")
        updated = AttioClient._deal_last_updated(deal)
        assert updated is not None
        assert updated.year == 2025
        assert updated.month == 1


class TestFormatDealLine:
    def test_full_format(self):
        deal = _make_deal(name="Nike", probability=75, value=50000, close_date="2025-05-31")
        line = AttioClient.format_deal_line(deal)
        assert "Nike" in line
        assert "75%" in line
        assert "$50,000" in line
        assert "May 31" in line

    def test_missing_value_omitted(self):
        deal = _make_deal(name="Adidas", value=0)
        deal["values"]["value"] = []
        line = AttioClient.format_deal_line(deal)
        assert "Adidas" in line
        assert "$" not in line

    def test_show_owner(self):
        deal = _make_deal(name="Puma", owner="Louis")
        line = AttioClient.format_deal_line(deal, show_owner=True)
        assert "Louis" in line


class TestFindDealByName:
    """Tests for fuzzy name matching (sync helper, no Attio API call)."""

    def _run(self, coro):
        import asyncio
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_exact_match_found(self, monkeypatch):
        deals = [_make_deal("Nike"), _make_deal("Adidas")]

        async def mock_list(*a, **kw):
            return deals

        monkeypatch.setattr("clients.attio.attio.list_deals", mock_list)
        from clients.attio import attio

        result = self._run(attio.find_deal_by_name("Nike"))
        assert result is not None
        assert AttioClient._deal_name(result) == "Nike"

    def test_partial_match_found(self, monkeypatch):
        deals = [_make_deal("Nike Global Campaign")]

        async def mock_list(*a, **kw):
            return deals

        monkeypatch.setattr("clients.attio.attio.list_deals", mock_list)
        from clients.attio import attio

        result = self._run(attio.find_deal_by_name("nike global"))
        assert result is not None

    def test_no_match_returns_none(self, monkeypatch):
        deals = [_make_deal("Nike"), _make_deal("Adidas")]

        async def mock_list(*a, **kw):
            return deals

        monkeypatch.setattr("clients.attio.attio.list_deals", mock_list)
        from clients.attio import attio

        result = self._run(attio.find_deal_by_name("Puma"))
        assert result is None
