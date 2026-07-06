from __future__ import annotations

import hashlib
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse
from telegram import BotCommand, Update

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig

from app.config import settings, validate_startup_settings
from app.jobs.daily_message import run_daily_message
from app.jobs.health_reminder_job import run_health_reminders
from app.jobs.proactive_check import run_proactive_check
from app.jobs.supplement_reminder import run_supplement_reminder
from app.jobs.weekly_message import run_weekly_message
from app.routes import whoop_oauth, withings_oauth, withings_webhook
from app.telegram.bot import build_application, get_application

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# Webhook secret: SHA-256 hex of SECRET_KEY (64 chars, only hex digits — always valid for Telegram)
_WEBHOOK_SECRET = hashlib.sha256(settings.secret_key.encode()).hexdigest()


def _run_migrations() -> None:
    cfg = AlembicConfig("alembic.ini")
    alembic_command.upgrade(cfg, "head")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    _run_migrations()
    validate_startup_settings()

    application = build_application()
    await application.initialize()
    await application.bot.set_webhook(
        url=f"{settings.base_url}/telegram/webhook",
        secret_token=_WEBHOOK_SECRET,
        allowed_updates=list(Update.ALL_TYPES),
        drop_pending_updates=True,
    )
    await application.bot.set_my_commands([
        # Daily use
        BotCommand("today", "Get your coach message for today"),
        BotCommand("checkin", "Log what happened yesterday"),
        BotCommand("creatine", "Log creatine taken today"),
        BotCommand("reta", "Log/track your retatrutide shot reminder"),
        BotCommand("focus", "One action item for this week"),
        BotCommand("weekly", "This week's summary"),
        # Review & track
        BotCommand("manual", "Your personal body manual & patterns"),
        BotCommand("memory", "What I've learned about you"),
        BotCommand("recs", "Recommendations I've made & how they went"),
        BotCommand("history", "Last 7 daily messages"),
        BotCommand("chatlog", "Full chat history for review"),
        BotCommand("experiment", "Start or check an experiment"),
        # Account & setup
        BotCommand("goal", "View or change your coaching goal"),
        BotCommand("goalweight", "Set your target weight"),
        BotCommand("timezone", "View or change your timezone"),
        BotCommand("connect_whoop", "Connect your WHOOP account"),
        BotCommand("connect_withings", "Connect your Withings scale"),
        BotCommand("backfill", "Re-pull 365 days of WHOOP + Withings data"),
        BotCommand("delete", "Permanently delete all your data"),
        BotCommand("start", "Set up your account"),
        BotCommand("help", "What this bot can do"),
    ])
    await application.start()

    scheduler = AsyncIOScheduler(timezone=ZoneInfo(settings.default_timezone))
    # Every scheduled job is wrapped in logged_job so each run is recorded in the
    # job_runs table (Fix #2) — name, start/finish, success/error.
    from functools import partial
    from app.services.job_log import logged_job

    def _job(job_name, fn):
        return partial(logged_job, job_name, fn)

    # Wake-aware morning message: poll every MORNING_WATCH_INTERVAL_MINUTES.
    # run_daily_message decides per user whether their LOCAL morning watch window
    # has arrived and whether their main sleep is ready — tz-aware and DST-correct
    # via zoneinfo. max_instances > 1 so a slow per-user send never blocks the
    # tick serving a user in another timezone.
    from app.jobs.daily_message import morning_cron_minute_spec
    scheduler.add_job(
        _job("daily_morning_message", run_daily_message),
        CronTrigger(minute=morning_cron_minute_spec(settings.morning_watch_interval_minutes)),
        id="daily_morning_message",
        max_instances=6,
        coalesce=True,
        misfire_grace_time=600,
    )
    # Noon + 9pm creatine nudge, same hourly-tick/per-user-timezone pattern as
    # the morning message. max_instances > 1 for the same cross-timezone reason.
    scheduler.add_job(
        _job("supplement_reminder", run_supplement_reminder),
        CronTrigger(minute=0),
        id="supplement_reminder",
        max_instances=6,
        coalesce=True,
        misfire_grace_time=600,
    )
    # Sunday-evening weekly summary — same hourly-tick pattern, gated to one day a week.
    scheduler.add_job(
        _job("weekly_summary", run_weekly_message),
        CronTrigger(minute=0),
        id="weekly_summary",
        max_instances=6,
        coalesce=True,
        misfire_grace_time=600,
    )
    # Gated proactive ping: fires when recovery is low 3+ days in a row, after
    # the morning message has gone out, during reasonable hours only.
    scheduler.add_job(
        _job("proactive_check", run_proactive_check),
        CronTrigger(minute=0),
        id="proactive_check",
        max_instances=3,
        coalesce=True,
        misfire_grace_time=600,
    )
    # Recurring health reminders (e.g. retatrutide every N days). Hourly tick,
    # per-user local clock; once-per-due-date via last_reminded_date.
    scheduler.add_job(
        _job("health_reminders", run_health_reminders),
        CronTrigger(minute=0),
        id="health_reminders",
        max_instances=3,
        coalesce=True,
        misfire_grace_time=600,
    )
    scheduler.start()

    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        await application.stop()
        await application.shutdown()


app = FastAPI(title="Body Manual AI", lifespan=lifespan)
app.include_router(whoop_oauth.router)
app.include_router(withings_oauth.router)
app.include_router(withings_webhook.router)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_policy() -> HTMLResponse:
    path = Path(__file__).parent.parent / "docs" / "privacy-policy.html"
    return HTMLResponse(content=path.read_text(encoding="utf-8"))


@app.get("/debug/db")
async def debug_db() -> dict:
    """Check which tables exist and row counts — useful for verifying migrations ran."""
    from sqlalchemy import inspect, text
    from app.db import engine
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    counts: dict[str, int] = {}
    with engine.connect() as conn:
        for t in tables:
            row = conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
            counts[t] = int(row or 0)
    return {"tables": tables, "row_counts": counts}


@app.get("/debug/webhook")
async def debug_webhook() -> dict:
    info = await get_application().bot.get_webhook_info()
    return {
        "url": info.url,
        "pending_update_count": info.pending_update_count,
        "last_error_date": str(info.last_error_date) if info.last_error_date else None,
        "last_error_message": info.last_error_message,
    }


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> Response:
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token", "") != _WEBHOOK_SECRET:
        return Response(status_code=403)
    data = await request.json()
    update = Update.de_json(data, get_application().bot)
    await get_application().process_update(update)
    return Response(status_code=200)
