"""
Scheduler — APScheduler cron jobs for all timed automations.
All jobs are registered here and started from main.py.
"""
from __future__ import annotations

import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

import config

logger = logging.getLogger(__name__)

TZ = pytz.timezone(config.TIMEZONE)


def build_scheduler(slack_client) -> AsyncIOScheduler:
    """
    Create and configure the APScheduler instance.
    `slack_client` is the Slack AsyncWebClient (injected from main.py).
    Returns an un-started scheduler — call .start() in main.py.
    """
    scheduler = AsyncIOScheduler(timezone=TZ)

    # ── Layer A: Data Freshness ────────────────────────────────────────

    # A3: Email signal scan — every 4 hours
    from handlers.a3_email_signals import run_email_scan
    scheduler.add_job(
        run_email_scan,
        CronTrigger(hour="*/4", timezone=TZ),
        args=[slack_client],
        id="a3_email_scan",
        name="A3 Email Signal Scan",
        misfire_grace_time=300,
    )

    # A4: Calendar pre-meeting nudge — every 30 min during work hours
    from handlers.a4_calendar_nudge import run_calendar_nudge
    scheduler.add_job(
        run_calendar_nudge,
        CronTrigger(minute="*/30", hour="7-19", timezone=TZ),
        args=[slack_client],
        id="a4_calendar_nudge",
        name="A4 Calendar Nudge",
        misfire_grace_time=120,
    )

    # A5: Weekly hygiene nudges — Monday 9 AM
    from handlers.a5_hygiene_nudge import run_hygiene_nudges
    scheduler.add_job(
        run_hygiene_nudges,
        CronTrigger(day_of_week="mon", hour=9, minute=0, timezone=TZ),
        args=[slack_client],
        id="a5_hygiene_nudge",
        name="A5 Weekly Hygiene Nudge",
        misfire_grace_time=600,
    )

    # ── Layer B: #deal-radar ──────────────────────────────────────────

    # B1: Monday Pipeline Forecast — Monday 9:05 AM (after hygiene nudges)
    from handlers.b1_monday_forecast import post_monday_forecast
    scheduler.add_job(
        post_monday_forecast,
        CronTrigger(day_of_week="mon", hour=9, minute=5, timezone=TZ),
        args=[slack_client],
        id="b1_monday_forecast",
        name="B1 Monday Forecast",
        misfire_grace_time=600,
    )

    # B2: Deal movement check — every 15 min
    from handlers.b2_deal_movement import run_deal_movement_check
    scheduler.add_job(
        run_deal_movement_check,
        CronTrigger(minute="*/15", timezone=TZ),
        args=[slack_client],
        id="b2_deal_movement",
        name="B2 Deal Movement Check",
        misfire_grace_time=120,
    )

    # B4: Production handoff check — every 15 min (mirrors B2)
    from handlers.b4_production_handoff import check_and_post_handoffs
    scheduler.add_job(
        check_and_post_handoffs,
        CronTrigger(minute="*/15", timezone=TZ),
        args=[slack_client],
        id="b4_handoff_check",
        name="B4 Production Handoff Check",
        misfire_grace_time=120,
    )

    # B5: Capacity warning — daily at 8 AM
    from handlers.b5_capacity_warning import run_capacity_check
    scheduler.add_job(
        run_capacity_check,
        CronTrigger(hour=8, minute=0, timezone=TZ),
        args=[slack_client],
        id="b5_capacity_check",
        name="B5 Capacity Conflict Check",
        misfire_grace_time=300,
    )

    # ── Layer C: Notion ───────────────────────────────────────────────

    # C: Attio → Notion daily sync — 7 AM every day (skipped if NOTION_TOKEN not set)
    if config.NOTION_TOKEN:
        from notion_sync.daily_sync import run_daily_sync
        scheduler.add_job(
            run_daily_sync,
            CronTrigger(hour=7, minute=0, timezone=TZ),
            args=[slack_client],
            id="c_notion_sync",
            name="C Notion Daily Sync",
            misfire_grace_time=600,
        )
    else:
        logger.info("Notion not configured — skipping C Notion Daily Sync job.")

    logger.info("Scheduler configured with %d jobs.", len(scheduler.get_jobs()))
    return scheduler
