"""
smoke_test.py — End-to-end connection check for Rabbit.

Run BEFORE starting the bot to verify every API is reachable and correctly
configured. Does NOT send any Slack messages or mutate any data.

Usage:
    python smoke_test.py            # check all systems
    python smoke_test.py --fast     # skip slow web-search check
"""
from __future__ import annotations

import asyncio
import sys
import os
import argparse
import logging
from datetime import datetime, timezone, timedelta

# ─── Suppress noisy logs during smoke test ───────────────────────────────────
logging.basicConfig(level=logging.WARNING)

PASS = "✅"
FAIL = "❌"
WARN = "⚠️ "

results: list[tuple[str, bool, str]] = []


def record(system: str, ok: bool, detail: str = ""):
    icon = PASS if ok else FAIL
    print(f"  {icon}  {system:<30} {detail}")
    results.append((system, ok, detail))


# ─── 1. Environment variables ─────────────────────────────────────────────────

def check_env() -> bool:
    print("\n[1] Environment variables")
    print("─" * 55)

    required = [
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "SLACK_SIGNING_SECRET",
        "DEAL_RADAR_CHANNEL_ID",
        "ATTIO_API_KEY",
        "NOTION_TOKEN",
        "NOTION_PRODUCTION_DB_ID",
        "ANTHROPIC_API_KEY",
    ]
    optional = [
        "GOOGLE_CREDENTIALS_FILE",
        "GOOGLE_TOKEN_FILE",
        "GOOGLE_ACCOUNT_EMAIL",
        "WEBHOOK_SECRET",
        "DEAL_TEAM_CHANNEL_ID",
    ]

    all_ok = True
    for key in required:
        val = os.getenv(key, "")
        ok = bool(val and val not in ("...", "change-me-to-a-random-secret"))
        record(key, ok, "(set)" if ok else "MISSING — required")
        if not ok:
            all_ok = False

    for key in optional:
        val = os.getenv(key, "")
        ok = bool(val)
        record(key, ok, "(set)" if ok else "not set (optional)")

    return all_ok


# ─── 2. Anthropic ─────────────────────────────────────────────────────────────

async def check_anthropic() -> bool:
    print("\n[2] Anthropic / Claude")
    print("─" * 55)
    try:
        import anthropic
        import config
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=5,
            messages=[{"role": "user", "content": "ping"}],
        )
        record("Anthropic API", True, f"model={resp.model}")
        return True
    except Exception as exc:
        record("Anthropic API", False, str(exc)[:80])
        return False


# ─── 3. Attio ─────────────────────────────────────────────────────────────────

async def check_attio() -> bool:
    print("\n[3] Attio CRM")
    print("─" * 55)
    try:
        from clients.attio import attio
        deals = await attio.list_deals(limit=3)
        record("Attio list_deals", True, f"{len(deals)} deal(s) returned")
        return True
    except Exception as exc:
        record("Attio list_deals", False, str(exc)[:80])
        return False


# ─── 4. Notion ────────────────────────────────────────────────────────────────

async def check_notion() -> bool:
    print("\n[4] Notion Production Calendar")
    print("─" * 55)
    try:
        import config
        from notion_client import AsyncClient
        nc = AsyncClient(auth=config.NOTION_TOKEN)
        db = await nc.databases.retrieve(database_id=config.NOTION_PRODUCTION_DB_ID)
        title = db.get("title", [{}])[0].get("plain_text", "?")
        record("Notion DB retrieve", True, f'DB: "{title}"')

        from clients.notion import notion_db
        pages = await notion_db.get_all_pages()
        record("Notion get_all_pages", True, f"{len(pages)} page(s) in calendar")
        return True
    except Exception as exc:
        record("Notion", False, str(exc)[:80])
        return False


# ─── 5. Slack ─────────────────────────────────────────────────────────────────

async def check_slack() -> bool:
    print("\n[5] Slack")
    print("─" * 55)
    try:
        import config
        from slack_sdk.web.async_client import AsyncWebClient
        sc = AsyncWebClient(token=config.SLACK_BOT_TOKEN)
        resp = await sc.auth_test()
        record("Slack auth_test", True, f"bot=@{resp['user']} team={resp['team']}")

        # Verify deal-radar channel exists
        chan = await sc.conversations_info(channel=config.DEAL_RADAR_CHANNEL_ID)
        name = chan["channel"]["name"]
        record("DEAL_RADAR_CHANNEL_ID", True, f"#{name}")
        return True
    except Exception as exc:
        record("Slack", False, str(exc)[:80])
        return False


# ─── 6. Google (Gmail + Calendar) ────────────────────────────────────────────

async def check_google() -> bool:
    print("\n[6] Google (Gmail + Calendar)")
    print("─" * 55)
    try:
        import config
        token_path = config.GOOGLE_TOKEN_FILE
        creds_path = config.GOOGLE_CREDENTIALS_FILE

        if not os.path.exists(creds_path):
            record("credentials.json", False,
                   f"Not found at {creds_path} — download from Google Cloud Console")
            return False
        record("credentials.json", True, f"found at {creds_path}")

        if not os.path.exists(token_path):
            record("token.json", False,
                   "Not found — run: python -m clients.google_auth")
            return False
        record("token.json", True, f"found at {token_path}")

        # Test Gmail
        from clients.gmail import gmail
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        signals = gmail.scan_for_deal_signals(after_timestamp=since, max_results=1)
        record("Gmail scan_for_deal_signals", True,
               f"{len(signals)} signal(s) in last 24h")

        # Test GCal
        from clients.gcal import gcal
        meetings = gcal.get_upcoming_prospect_meetings(hours_ahead=48)
        record("GCal get_upcoming_meetings", True,
               f"{len(meetings)} meeting(s) in next 48h")

        return True
    except Exception as exc:
        record("Google", False, str(exc)[:80])
        return False


# ─── 7. Rabbit agent smoke (no real Claude call) ──────────────────────────────

def check_agent_imports() -> bool:
    print("\n[7] Agent imports")
    print("─" * 55)
    agents = [
        ("agents.viktor_tool_agent", "run_viktor"),
        ("agents.research_agent", "run_research_agent"),
        ("agents.production_planner_agent", "run_production_planner"),
        ("agents.signal_agent", "run_signal_agent"),
    ]
    all_ok = True
    for module, fn in agents:
        try:
            mod = __import__(module, fromlist=[fn])
            getattr(mod, fn)
            record(f"{module}.{fn}", True, "importable")
        except Exception as exc:
            record(f"{module}.{fn}", False, str(exc)[:80])
            all_ok = False
    return all_ok


# ─── 8. Scheduler import ──────────────────────────────────────────────────────

def check_scheduler() -> bool:
    print("\n[8] Scheduler")
    print("─" * 55)
    try:
        from scheduler import build_scheduler
        record("scheduler.build_scheduler", True, "importable")
        return True
    except Exception as exc:
        record("scheduler.build_scheduler", False, str(exc)[:80])
        return False


# ─── Summary ──────────────────────────────────────────────────────────────────

def print_summary():
    print("\n" + "═" * 55)
    print("SUMMARY")
    print("═" * 55)
    failures = [(s, d) for s, ok, d in results if not ok]
    if not failures:
        print(f"\n{PASS} All checks passed — Rabbit is ready to run!\n")
        print("  Start:    python3 main.py")
        print("  With PM:  supervisord -c supervisord.conf\n")
    else:
        print(f"\n{FAIL} {len(failures)} check(s) failed:\n")
        for sys_name, detail in failures:
            print(f"  • {sys_name}: {detail}")
        print(
            "\nFix the above issues, then re-run: python smoke_test.py\n"
        )
    return len(failures) == 0


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main(fast: bool = False):
    print("\n🐰 Rabbit Smoke Test")
    print("=" * 55)
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # Load .env first
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        print(f"\n{FAIL} python-dotenv not installed. Run: pip install -r requirements.txt")
        sys.exit(1)

    env_ok = check_env()
    if not env_ok:
        print(f"\n{FAIL} Fix missing required env vars in .env before continuing.")
        sys.exit(1)

    await check_anthropic()
    await check_attio()
    await check_notion()
    await check_slack()

    if not fast:
        await check_google()
    else:
        print("\n[6] Google  — skipped (--fast)")

    check_agent_imports()
    check_scheduler()

    success = print_summary()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Rabbit end-to-end smoke test")
    parser.add_argument("--fast", action="store_true",
                        help="Skip Google OAuth check (faster, for CI)")
    args = parser.parse_args()
    asyncio.run(main(fast=args.fast))
