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

from app.config import settings, validate_startup_settings
from app.jobs.daily_message import run_daily_message
from app.routes import whoop_oauth, withings_oauth
from app.telegram.bot import build_application, get_application

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# Webhook secret: SHA-256 hex of SECRET_KEY (64 chars, only hex digits — always valid for Telegram)
_WEBHOOK_SECRET = hashlib.sha256(settings.secret_key.encode()).hexdigest()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
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
        BotCommand("today", "Get your coach message for today"),
        BotCommand("checkin", "Log what happened yesterday"),
        BotCommand("weekly", "This week's summary"),
        BotCommand("manual", "Your personal body manual"),
        BotCommand("connect_whoop", "Connect your WHOOP account"),
        BotCommand("connect_withings", "Connect your Withings scale"),
        BotCommand("backfill", "Re-pull 365 days of WHOOP data"),
        BotCommand("delete", "Permanently delete all your data"),
        BotCommand("start", "Set up your account"),
    ])
    await application.start()

    scheduler = AsyncIOScheduler(timezone=ZoneInfo(settings.default_timezone))
    scheduler.add_job(
        run_daily_message,
        CronTrigger(hour=settings.daily_pull_hour, minute=0),
        id="daily_morning_message",
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


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_policy() -> HTMLResponse:
    path = Path(__file__).parent.parent / "docs" / "privacy-policy.html"
    return HTMLResponse(content=path.read_text(encoding="utf-8"))


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
