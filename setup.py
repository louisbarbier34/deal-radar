"""
One-time setup script.
Run: python setup.py

Does:
1. Copies .env.example → .env (if not exists)
2. Installs dependencies
3. Authenticates with Google (browser OAuth flow)
4. Verifies Attio + Notion + Slack connections
5. Ensures Notion DB has all required properties
"""
from __future__ import annotations

import os
import subprocess
import sys


def step(n: int, msg: str):
    print(f"\n[{n}] {msg}")
    print("─" * 50)


def run(cmd: str):
    result = subprocess.run(cmd, shell=True)
    if result.returncode != 0:
        print(f"Command failed: {cmd}")
        sys.exit(1)


def main():
    print("\n🐰 Rabbit Setup")
    print("=" * 50)

    # 1. .env
    step(1, "Environment file")
    if not os.path.exists(".env"):
        import shutil
        shutil.copy(".env.example", ".env")
        print("Created .env from .env.example")
        print("👉 Open .env and fill in your API keys, then re-run setup.py")
        sys.exit(0)
    else:
        print(".env already exists.")

    # 2. Dependencies
    step(2, "Installing dependencies")
    run(f"{sys.executable} -m pip install -r requirements.txt -q")
    print("Dependencies installed.")

    # Load env vars now that deps are available
    from dotenv import load_dotenv
    load_dotenv()

    # 3. Google OAuth
    step(3, "Google OAuth (Gmail + Calendar)")
    if not os.path.exists(os.getenv("GOOGLE_TOKEN_FILE", "token.json")):
        creds_file = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
        if not os.path.exists(creds_file):
            print(f"⚠️  Missing {creds_file}")
            print(
                "Download it from Google Cloud Console → APIs & Services → Credentials\n"
                "Enable Gmail API and Google Calendar API first."
            )
        else:
            run(f"{sys.executable} -m clients.google_auth")
    else:
        print("Google token already exists.")

    # 4. Connection checks
    step(4, "Verifying API connections")
    import asyncio
    asyncio.run(_verify_connections())

    # 5. Attio attribute mapping validation
    step(5, "Validating Attio attribute mapping")
    asyncio.run(validate_attio_mapping())

    # 6. Notion DB properties
    step(6, "Ensuring Notion database properties")
    asyncio.run(_setup_notion())

    print(
        "\n✅ Setup complete!\n"
        "Run: python3 main.py\n"
        "Or with supervisord: supervisord -c supervisord.conf"
    )


async def _verify_connections():
    errors = []

    # Attio
    try:
        from clients.attio import attio
        deals = await attio.list_deals(limit=1)
        print(f"✅ Attio — connected ({len(deals)} deal fetched)")
    except Exception as e:
        print(f"❌ Attio — {e}")
        errors.append("attio")

    # Notion
    try:
        from notion_client import AsyncClient
        import config
        nc = AsyncClient(auth=config.NOTION_TOKEN)
        db = await nc.databases.retrieve(database_id=config.NOTION_PRODUCTION_DB_ID)
        print(f"✅ Notion — connected (DB: {db.get('title', [{}])[0].get('plain_text', '?')})")
    except Exception as e:
        print(f"❌ Notion — {e}")
        errors.append("notion")

    # Slack
    try:
        import config
        from slack_sdk.web.async_client import AsyncWebClient
        sc = AsyncWebClient(token=config.SLACK_BOT_TOKEN)
        resp = await sc.auth_test()
        print(f"✅ Slack — connected as @{resp['user']} in {resp['team']}")
    except Exception as e:
        print(f"❌ Slack — {e}")
        errors.append("slack")

    # Anthropic
    try:
        import config
        import anthropic
        c = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        r = c.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content": "ping"}],
        )
        print("✅ Anthropic — connected")
    except Exception as e:
        print(f"❌ Anthropic — {e}")
        errors.append("anthropic")

    if errors:
        print(f"\n⚠️  Fix these connections before running Rabbit: {', '.join(errors)}")


async def _setup_notion():
    try:
        from clients.notion import notion_db
        await notion_db.ensure_database_properties()
        print("✅ Notion DB properties verified.")
    except Exception as e:
        print(f"❌ Notion DB setup failed: {e}")


async def validate_attio_mapping():
    """
    Fetch one deal from Attio and assert all expected attribute slugs exist.
    Fails loudly if the workspace uses different field names.
    """
    from clients.attio import attio, AttioClient

    REQUIRED_ATTRS = {
        "name": "Deal name",
        "stage": "Stage",
        "probability": "Win probability (0–100)",
        "value": "Deal value",
        "close_date": "Close date",
        "owner": "Deal owner",
    }

    try:
        deals = await attio.list_deals(limit=1)
    except Exception as e:
        print(f"❌ Attio attribute validation — could not fetch deals: {e}")
        return

    if not deals:
        print("⚠️  Attio attribute validation skipped — no deals found in workspace.")
        return

    deal = deals[0]
    values = deal.get("values", {})
    missing = []

    for slug, label in REQUIRED_ATTRS.items():
        val = AttioClient._attr(deal, slug)
        if val is None and slug not in values:
            missing.append(f"  - '{slug}' ({label})")

    if missing:
        print(
            "❌ Attio attribute mapping mismatch!\n"
            "The following expected attribute slugs were not found in your Attio workspace:\n"
            + "\n".join(missing)
            + "\n\nFix: update ATTIO_DEAL_OBJECT in .env or rename attributes in your Attio workspace."
        )
        sys.exit(1)
    else:
        print("✅ Attio attribute mapping validated.")


if __name__ == "__main__":
    main()
