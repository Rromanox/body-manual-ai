"""WHOOP OAuth redirect handling — the only web route in the MVP."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from app.db import SessionLocal
from app.jobs.daily_pull import pull_and_store
from app.models.oauth_connection import OAuthConnection
from app.models.user import User
from app.services.alerts import send_admin_alert
from app.services.whoop_client import (
    SCOPES,
    WhoopApiError,
    WhoopAuthError,
    apply_token_response,
    exchange_code,
    verify_oauth_state,
)
from app.telegram.bot import get_application

logger = logging.getLogger(__name__)

router = APIRouter()

BACKFILL_DAYS = 30


@router.get("/auth/whoop/callback", response_class=HTMLResponse)
async def whoop_oauth_callback(
    code: str | None = None, state: str | None = None, error: str | None = None
) -> HTMLResponse:
    if error:
        return _page("WHOOP authorization was canceled. Send /connect_whoop in Telegram to try again.", 400)
    telegram_id = verify_oauth_state(state)
    if telegram_id is None:
        return _page("This connect link is invalid or expired. Send /connect_whoop for a fresh one.", 400)
    if not code:
        return _page("Missing authorization code — send /connect_whoop and try again.", 400)

    try:
        token_data = await exchange_code(code)
    except (WhoopAuthError, WhoopApiError) as exc:
        logger.exception("WHOOP code exchange failed for telegram_id=%s", telegram_id)
        await send_admin_alert(f"WHOOP code exchange failed for telegram_id={telegram_id}: {exc}")
        return _page("Connecting to WHOOP failed. Send /connect_whoop to try again.", 502)

    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            return _page("No account found — send /start to the bot first, then /connect_whoop.", 400)
        connection = session.scalar(
            select(OAuthConnection).where(
                OAuthConnection.user_id == user.id, OAuthConnection.provider == "whoop"
            )
        )
        if connection is None:
            connection = OAuthConnection(
                user_id=user.id, provider="whoop", access_token="", refresh_token=""
            )
            session.add(connection)
        apply_token_response(connection, token_data)
        connection.scopes = token_data.get("scope", SCOPES)
        session.commit()
        user_id = user.id

    asyncio.create_task(_notify_and_backfill(user_id, telegram_id))
    return _page("WHOOP connected ✅ You can close this tab and head back to Telegram.")


async def _notify_and_backfill(user_id: int, telegram_id: int) -> None:
    bot = get_application().bot
    await bot.send_message(
        chat_id=telegram_id,
        text=f"WHOOP connected ✅ Pulling your last {BACKFILL_DAYS} days so I can start building your baseline…",
    )
    try:
        with SessionLocal() as session:
            user = session.get(User, user_id)
            if user is None:
                return
            written = await pull_and_store(session, user, days=BACKFILL_DAYS)
        await bot.send_message(
            chat_id=telegram_id,
            text=f"Done — {written} days of data loaded. Try /today for your first coach message.",
        )
    except Exception as exc:
        logger.exception("Post-connect backfill failed for user %s", user_id)
        await send_admin_alert(f"Post-connect WHOOP backfill failed for user {user_id}: {exc}")
        await bot.send_message(
            chat_id=telegram_id,
            text="I hit a snag pulling your WHOOP history — I've flagged it. /today will still try a fresh pull.",
        )


def _page(message: str, status_code: int = 200) -> HTMLResponse:
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Body Manual AI</title>
<style>body{{font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;
height:100vh;margin:0;background:#fafafa;color:#222}}div{{max-width:26rem;text-align:center;padding:2rem}}</style>
</head><body><div><h2>Body Manual AI</h2><p>{message}</p></div></body></html>"""
    return HTMLResponse(content=html, status_code=status_code)
