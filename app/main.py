from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from telegram import Update

from app.config import settings, validate_startup_settings
from app.jobs.daily_pull import run_daily_pull
from app.routes import whoop_oauth
from app.telegram.bot import build_application

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    validate_startup_settings()

    application = build_application()
    await application.initialize()
    await application.start()
    await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)

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
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


app = FastAPI(title="Body Manual AI", lifespan=lifespan)
app.include_router(whoop_oauth.router)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
