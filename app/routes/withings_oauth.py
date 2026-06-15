"""Withings OAuth callback route."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from app.db import SessionLocal
from app.models.daily_metric import DailyMetric
from app.models.oauth_connection import OAuthConnection
from app.models.user import User
from app.services.alerts import send_admin_alert
from app.services.withings_client import (
    WithingsApiError,
    WithingsAuthError,
    apply_token_response,
    ensure_fresh_access_token,
    exchange_code,
    normalize_measurements,
    pull_body_measurements,
    subscribe_notifications,
)
from app.services.whoop_client import verify_oauth_state
from app.telegram.bot import get_application

logger = logging.getLogger(__name__)

router = APIRouter()

BACKFILL_DAYS = 365


@router.get("/auth/withings/callback", response_class=HTMLResponse)
async def withings_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    if error:
        return _page("Withings authorization was canceled. Send /connect_withings in Telegram to try again.", 400)
    telegram_id = verify_oauth_state(state)
    if telegram_id is None:
        return _page("This connect link is invalid or expired. Send /connect_withings for a fresh one.", 400)
    if not code:
        return _page("Missing authorization code — send /connect_withings and try again.", 400)

    try:
        token_data = await exchange_code(code)
    except (WithingsAuthError, WithingsApiError) as exc:
        logger.exception("Withings code exchange failed for telegram_id=%s", telegram_id)
        await send_admin_alert(f"Withings code exchange failed for telegram_id={telegram_id}: {exc}")
        return _page("Connecting to Withings failed. Send /connect_withings to try again.", 502)

    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            return _page("No account found — send /start to the bot first, then /connect_withings.", 400)
        connection = session.scalar(
            select(OAuthConnection).where(
                OAuthConnection.user_id == user.id, OAuthConnection.provider == "withings"
            )
        )
        if connection is None:
            connection = OAuthConnection(
                user_id=user.id, provider="withings", access_token="", refresh_token=""
            )
            session.add(connection)
        apply_token_response(connection, token_data)
        session.commit()
        user_id = user.id

    asyncio.create_task(_notify_and_backfill(user_id, telegram_id))
    return _page("Withings connected ✅ You can close this tab and head back to Telegram.")


async def _notify_and_backfill(user_id: int, telegram_id: int) -> None:
    bot = get_application().bot
    await bot.send_message(
        chat_id=telegram_id,
        text=f"Withings connected ✅ Pulling your last {BACKFILL_DAYS} days of body measurements…",
    )
    try:
        written = await pull_withings_and_store(user_id, days=BACKFILL_DAYS)
        # Subscribe to push notifications so we get updates when user steps on scale
        with SessionLocal() as session:
            from sqlalchemy import select as _select
            conn = session.scalar(
                _select(OAuthConnection).where(
                    OAuthConnection.user_id == user_id,
                    OAuthConnection.provider == "withings",
                    OAuthConnection.status == "active",
                )
            )
            if conn:
                access_token = await ensure_fresh_access_token(session, conn)
                try:
                    await subscribe_notifications(access_token)
                except Exception as sub_exc:
                    logger.warning("Withings notification subscribe failed: %s", sub_exc)
        await bot.send_message(
            chat_id=telegram_id,
            text=f"Done — body composition loaded for {written} days. I'll update automatically whenever you step on the scale.",
        )
    except Exception as exc:
        logger.exception("Post-connect Withings backfill failed for user %s", user_id)
        await send_admin_alert(f"Post-connect Withings backfill failed for user {user_id}: {exc}")
        await bot.send_message(
            chat_id=telegram_id,
            text="I hit a snag pulling your Withings history — I've flagged it. /today will still work with your WHOOP data.",
        )


async def pull_withings_and_store(user_id: int, days: int = 7) -> int:
    """Pull Withings body measurements and upsert into daily_metrics. Returns days written."""
    with SessionLocal() as session:
        user = session.get(User, user_id)
        if user is None:
            return 0
        connection = session.scalar(
            select(OAuthConnection).where(
                OAuthConnection.user_id == user_id,
                OAuthConnection.provider == "withings",
                OAuthConnection.status == "active",
            )
        )
        if connection is None:
            return 0

        access_token = await ensure_fresh_access_token(session, connection)

        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        groups = await pull_body_measurements(access_token, start, end)

        measurements = normalize_measurements(groups, user.timezone)
        written = 0
        for meas_date, values in measurements.items():
            if not values:
                continue
            row = session.scalar(
                select(DailyMetric).where(
                    DailyMetric.user_id == user_id,
                    DailyMetric.date == meas_date,
                )
            )
            if row is None:
                row = DailyMetric(user_id=user_id, date=meas_date)
                session.add(row)

            for col, value in values.items():
                setattr(row, col, value)

            raw = row.raw_withings_json or {}
            raw[str(meas_date)] = values
            row.raw_withings_json = raw
            written += 1

        session.commit()
        return written


def _page(message: str, status_code: int = 200) -> HTMLResponse:
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Body Manual AI</title>
<style>body{{font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;
height:100vh;margin:0;background:#fafafa;color:#222}}div{{max-width:26rem;text-align:center;padding:2rem}}</style>
</head><body><div><h2>Body Manual AI</h2><p>{message}</p></div></body></html>"""
    return HTMLResponse(content=html, status_code=status_code)
