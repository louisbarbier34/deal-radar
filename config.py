"""Central config loaded from .env — import this everywhere."""
import os
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(f"Missing required env var: {key}")
    return val


# ─── Slack ────────────────────────────────────────────────────────────────────
SLACK_BOT_TOKEN = _require("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = _require("SLACK_APP_TOKEN")
SLACK_SIGNING_SECRET = _require("SLACK_SIGNING_SECRET")
DEAL_RADAR_CHANNEL_ID = _require("DEAL_RADAR_CHANNEL_ID")
DEAL_TEAM_CHANNEL_ID = os.getenv("DEAL_TEAM_CHANNEL_ID", DEAL_RADAR_CHANNEL_ID)

# ─── Attio ────────────────────────────────────────────────────────────────────
ATTIO_API_KEY = _require("ATTIO_API_KEY")
ATTIO_WORKSPACE_SLUG = os.getenv("ATTIO_WORKSPACE_SLUG", "wonder-studios")
ATTIO_DEAL_OBJECT = os.getenv("ATTIO_DEAL_OBJECT", "deals")
ATTIO_CONTACT_OBJECT = os.getenv("ATTIO_CONTACT_OBJECT", "people")
ATTIO_COMPANY_OBJECT = os.getenv("ATTIO_COMPANY_OBJECT", "companies")

# ─── Notion (optional — Notion features disabled if not set) ──────────────────
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")
NOTION_PRODUCTION_DB_ID = os.getenv("NOTION_PRODUCTION_DB_ID", "")

# ─── Anthropic ────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = _require("ANTHROPIC_API_KEY")

# ─── Google ───────────────────────────────────────────────────────────────────
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
GOOGLE_TOKEN_FILE = os.getenv("GOOGLE_TOKEN_FILE", "token.json")
GOOGLE_ACCOUNT_EMAIL = os.getenv("GOOGLE_ACCOUNT_EMAIL", "")

# ─── Database ─────────────────────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", "rabbit_state.db")

# ─── Google (base64 for cloud — decoded by start.sh) ──────────────────────────
GOOGLE_TOKEN_JSON       = os.getenv("GOOGLE_TOKEN_JSON", "")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "")

# ─── Webhook ──────────────────────────────────────────────────────────────────
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")   # Set in .env for production
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8765"))

# ─── App ──────────────────────────────────────────────────────────────────────
TIMEZONE = os.getenv("TIMEZONE", "America/New_York")
FORECAST_MIN_PROBABILITY = int(os.getenv("FORECAST_MIN_PROBABILITY", "30"))
STALE_DEAL_DAYS = int(os.getenv("STALE_DEAL_DAYS", "21"))
CAPACITY_LOOKAHEAD_DAYS = int(os.getenv("CAPACITY_LOOKAHEAD_DAYS", "90"))
