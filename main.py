"""
main.py — Rabbit (deal intelligence agent) entry point.
Starts the Slack Bolt app (Socket Mode) and the APScheduler.

Run: python main.py

Reaction status flow on every @Rabbit mention:
  👀 (eyes)             — message received, starting
  ⏳ (hourglass)        — actively processing / calling tools
  ✅ (white_check_mark) — done, response posted
  ❌ (x)                — error occurred
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys

from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_sdk.web.async_client import AsyncWebClient

import config
from scheduler import build_scheduler
from handlers.b2_deal_movement import seed_snapshot

# ─── Logging ──────────────────────────────────────────────────────────────────
_log_handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
if not os.getenv("RAILWAY_ENVIRONMENT"):   # skip file log in cloud
    _log_handlers.append(logging.FileHandler("rabbit.log"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=_log_handlers,
)
logger = logging.getLogger(__name__)

# ─── Slack App ────────────────────────────────────────────────────────────────
app = AsyncApp(token=config.SLACK_BOT_TOKEN)


# ─── Reaction helper ──────────────────────────────────────────────────────────

async def _react(
    client,
    channel: str,
    ts: str,
    add: str,
    remove: str | None = None,
) -> None:
    """
    Add a reaction emoji; optionally remove another first.
    All failures are silently swallowed — reactions are best-effort UX.
    """
    if remove:
        try:
            await client.reactions_remove(channel=channel, timestamp=ts, name=remove)
        except Exception:
            pass
    try:
        await client.reactions_add(channel=channel, timestamp=ts, name=add)
    except Exception:
        pass


# ─── @Rabbit app_mention handler ──────────────────────────────────────────────

@app.event("app_mention")
async def on_mention(event, say, client):
    channel: str = event.get("channel", "")
    ts: str = event.get("ts", "")
    text: str = event.get("text", "")
    user: str = event.get("user", "")
    thread_ts: str | None = event.get("thread_ts")

    clean = re.sub(r"<@[A-Z0-9]+>", "", text).strip()
    logger.info("@Rabbit mention from %s: %s", user, clean[:100])

    # 👀 Seen — instant acknowledgment before any async work
    await _react(client, channel, ts, add="eyes")

    if not clean:
        await _react(client, channel, ts, add="white_check_mark", remove="eyes")
        await say(
            "Hey! I'm Rabbit, your deal intelligence agent. :rabbit2:\n\n"
            "I'm connected to *Attio*, *Notion*, *Gmail*, and *Google Calendar*. Try:\n"
            "• `@Rabbit what deals close in May?`\n"
            "• `@Rabbit update Nike to 85%`\n"
            "• `@Rabbit any emails from clients this week?`\n"
            "• `@Rabbit what meetings do I have tomorrow?`\n"
            "• `@Rabbit what's in production right now?`"
        )
        return

    # ⏳ Processing — swap seen for hourglass
    await _react(client, channel, ts, add="hourglass_flowing_sand", remove="eyes")

    try:
        from agents.viktor_tool_agent import run_viktor
        await run_viktor(clean, say, client, user_id=user, thread_ts=thread_ts)
        # ✅ Done
        await _react(client, channel, ts, add="white_check_mark", remove="hourglass_flowing_sand")

    except Exception as exc:
        logger.error("Rabbit on_mention error: %s", exc)
        # ❌ Error
        await _react(client, channel, ts, add="x", remove="hourglass_flowing_sand")
        try:
            await say(
                text="_Something went wrong on my end. Try again or check rabbit.log._",
                **({"thread_ts": thread_ts} if thread_ts else {}),
            )
        except Exception:
            pass


# ─── Message handler (meeting recap detection) ────────────────────────────────

@app.event("message")
async def on_message(event, say, client):
    # Ignore bot messages, edits, and deletes
    if event.get("bot_id") or event.get("subtype"):
        return

    text: str = event.get("text", "")
    user: str = event.get("user", "")
    channel: str = event.get("channel", "")

    if len(text) < 80:
        return  # Too short to be a recap

    from handlers.a2_meeting_recap import handle_recap_message
    await handle_recap_message(text, say, channel, user)


# ─── Slack action handlers (button clicks) ────────────────────────────────────

@app.action("log_recap_to_attio")
async def action_log_recap(ack, body, say):
    await ack()
    value: str = body["actions"][0]["value"]
    parts = value.split("|||", 1)
    deal_name = parts[0].strip()
    note_text = parts[1].strip() if len(parts) > 1 else ""

    from handlers.a2_meeting_recap import log_recap_to_attio
    result = await log_recap_to_attio(deal_name, note_text)
    await say(result)


@app.action("dismiss_recap")
async def action_dismiss_recap(ack, body):
    await ack()


@app.action("log_email_signal_to_attio")
async def action_log_email(ack, body, say):
    await ack()
    value: str = body["actions"][0]["value"]
    parts = value.split("|||", 1)
    deal_name = parts[0].strip()
    note_text = parts[1].strip() if len(parts) > 1 else ""

    from handlers.a2_meeting_recap import log_recap_to_attio
    result = await log_recap_to_attio(deal_name, note_text)
    await say(result)


@app.action("dismiss_email_signal")
async def action_dismiss_email(ack, body):
    await ack()


# ─── Startup ──────────────────────────────────────────────────────────────────

async def main() -> None:
    logger.info("Starting Rabbit…")

    # Shared Slack client (for scheduler jobs that need to post messages)
    slack_client = AsyncWebClient(token=config.SLACK_BOT_TOKEN)

    # Seed the deal movement snapshot before the scheduler starts polling
    await seed_snapshot()

    # Build and start scheduler
    scheduler = build_scheduler(slack_client)
    scheduler.start()
    logger.info("Scheduler started.")

    # Start Slack Socket Mode handler
    handler = AsyncSocketModeHandler(app, config.SLACK_APP_TOKEN)
    logger.info("Rabbit is online. Listening for @mentions… 🐇")
    await handler.start_async()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Rabbit shut down.")
