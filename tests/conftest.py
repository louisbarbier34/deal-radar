"""
Shared pytest fixtures.

Dependency stubs
────────────────
The project depends on packages (anthropic, httpx, tenacity, notion-client,
duckduckgo-search, slack-bolt, etc.) that may not be installed in the bare
system Python used by pytest.  We stub them out here at the sys.modules level
*before* any agent/handler module is imported, so tests can run without
installing the full requirements.txt.
"""
from __future__ import annotations

import json
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest


# ─── Dependency stubs (must run before any project import) ───────────────────

def _stub(name: str, **attrs) -> MagicMock:
    """Create a MagicMock module stub and register it in sys.modules."""
    mod = MagicMock(name=name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules.setdefault(name, mod)
    return mod


# Core packages the agents/clients import at module level
_stub("anthropic")
_stub("httpx")
_stub("tenacity",
      retry=lambda **kw: (lambda f: f),           # @retry(...) → identity
      retry_if_exception=MagicMock(return_value=MagicMock()),
      stop_after_attempt=MagicMock(return_value=MagicMock()),
      wait_exponential=MagicMock(return_value=MagicMock()),
      before_sleep_log=MagicMock(return_value=MagicMock()),
      )
_stub("notion_client")
_stub("duckduckgo_search")
_stub("slack_bolt")
_stub("slack_bolt.async_app")
_stub("slack_bolt.adapter")
_stub("slack_bolt.adapter.socket_mode")
_stub("slack_bolt.adapter.socket_mode.async_handler")
_stub("slack_sdk")
_stub("slack_sdk.web")
_stub("slack_sdk.web.async_client")
_stub("apscheduler")
_stub("apscheduler.schedulers")
_stub("apscheduler.schedulers.asyncio")
_stub("apscheduler.triggers")
_stub("apscheduler.triggers.cron")
_stub("pytz")

# Stub the local scheduler module so importing main.py doesn't pull in pytz
_stub("scheduler", build_scheduler=MagicMock(return_value=MagicMock()))

# freezegun is a real install — do NOT stub it so tests get actual time-freezing

# Google API stubs
_stub("google")
_stub("google.auth")
_stub("google.auth.transport")
_stub("google.auth.transport.requests")
_stub("google.oauth2")
_stub("google.oauth2.credentials")
_stub("google_auth_oauthlib")
_stub("google_auth_oauthlib.flow")
_stub("googleapiclient")
_stub("googleapiclient.discovery")

# config stub (avoid missing .env)
config_stub = _stub("config",
    ANTHROPIC_API_KEY="test-key",
    ATTIO_API_KEY="test-attio-key",
    ATTIO_DEAL_OBJECT="deals",
    NOTION_TOKEN="test-notion-key",
    NOTION_PRODUCTION_DB_ID="test-db-id",
    SLACK_BOT_TOKEN="xoxb-test",
    SLACK_APP_TOKEN="xapp-test",
    DEAL_RADAR_CHANNEL_ID="C_TEST",
    STALE_DEAL_DAYS=14,
    FORECAST_MIN_PROBABILITY=50,
    CAPACITY_WARNING_MONTHS=2,
    GOOGLE_CREDENTIALS_FILE="creds.json",
    GOOGLE_TOKEN_FILE="token.json",
    DB_PATH=":memory:",               # in-memory SQLite for tests
    GOOGLE_TOKEN_JSON="",
    GOOGLE_CREDENTIALS_JSON="",
)

# ─── Claude async mock helpers ────────────────────────────────────────────────

def _make_claude_response(text: str):
    """Build a minimal fake AsyncAnthropic response (end_turn, text only)."""
    content = MagicMock()
    content.text = text
    resp = MagicMock()
    resp.content = [content]
    resp.stop_reason = "end_turn"
    return resp


@pytest.fixture
def mock_claude(monkeypatch):
    """
    Patch agents.viktor._claude with an AsyncMock.

    Usage:
        async def test_something(mock_claude):
            mock_claude.messages.create.return_value = _make_claude_response("...")
    """
    mock = MagicMock()
    mock.messages.create = AsyncMock()
    monkeypatch.setattr("agents.viktor._claude", mock)
    return mock


@pytest.fixture
def claude_json(mock_claude):
    """
    Set mock_claude to return a JSON string response.
    Usage: claude_json({"intent": "update_deal", ...})
    """
    def _set(data: dict):
        mock_claude.messages.create.return_value = _make_claude_response(
            json.dumps(data)
        )
    return _set


@pytest.fixture
def claude_text(mock_claude):
    """
    Set mock_claude to return a plain text response.
    Usage: claude_text("Pipeline looking strong.")
    """
    def _set(text: str):
        mock_claude.messages.create.return_value = _make_claude_response(text)
    return _set
