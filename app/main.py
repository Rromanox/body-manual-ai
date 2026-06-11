from __future__ import annotations

import hashlib
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Request, Response
from telegram import Update

from app.config import settings, validate_startup_settings
from app.jobs.daily_pull import run_daily_pull
from app.routes import whoop_oauth
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
    await application.start()

    scheduler = AsyncIOScheduler(timezone=ZoneInfo(settings.default_timezone))
    scheduler.add_job(
        run_daily_pull,
        CronTrigger(hour=settings.daily_pull_hour, minute=0),
        id="daily_whoop_pull",
    )
    scheduler.start()

    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        await application.bot.delete_webhook()
        await application.stop()
        await application.shutdown()


app = FastAPI(title="Body Manual AI", lifespan=lifespan)
app.include_router(whoop_oauth.router)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> Response:
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token", "") != _WEBHOOK_SECRET:
        return Response(status_code=403)
    data = await request.json()
    update = Update.de_json(data, get_application().bot)
    await get_application().process_update(update)
    return Response(status_code=200)
