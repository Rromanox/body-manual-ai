from __future__ import annotations

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from app.config import settings
from app.db import SessionLocal
from app.jobs.daily_pull import pull_and_store
from app.models.coach_message import CoachMessage
from app.models.oauth_connection import OAuthConnection
from app.models.user import User
from app.services.ai_client import generate_daily_message
from app.services.alerts import send_admin_alert
from app.services.baseline_engine import build_daily_snapshot, safety_message
from app.services.coach_payload_builder import build_daily_payload
from app.services.whoop_client import (
    WhoopAuthError,
    build_authorize_url,
    make_oauth_state,
)

logger = logging.getLogger(__name__)

CONSENT_MESSAGE = """\
Hey {name} — I'm your body coach. Before we start, here's the deal in plain English:

• I store the health data you connect: WHOOP recovery, sleep, heart rate, strain, and workouts.
• Each day I send a short, pre-computed summary of that data to an AI provider (OpenAI) to write your coach message. Your raw history never leaves this app.
• /delete erases everything I have about you, permanently. No copies, no archive.
• I'm a wellness coach, not a doctor. I never diagnose — and if something looks worth a professional opinion, I'll say exactly that.

If that works for you, connect your WHOOP with /connect_whoop. Once data is flowing, /today gets you your first coach message."""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    tg_user = update.effective_user
    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.telegram_id == tg_user.id))
        if user is None:
            user = User(telegram_id=tg_user.id, timezone=settings.default_timezone)
            session.add(user)
        user.first_name = tg_user.first_name
        user.username = tg_user.username
        session.commit()
    await update.message.reply_text(CONSENT_MESSAGE.format(name=tg_user.first_name or "there"))


async def connect_whoop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.telegram_id == update.effective_user.id))
    if user is None:
        await update.message.reply_text("Run /start first so I can set you up.")
        return
    url = build_authorize_url(make_oauth_state(update.effective_user.id))
    await update.message.reply_text(f"Tap to connect your WHOOP:\n\n{url}")


async def today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    telegram_id = update.effective_user.id
    await context.bot.send_chat_action(chat_id=update.message.chat_id, action=ChatAction.TYPING)

    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            await update.message.reply_text("Run /start first so I can set you up.")
            return
        connection = session.scalar(
            select(OAuthConnection).where(
                OAuthConnection.user_id == user.id, OAuthConnection.provider == "whoop"
            )
        )
        if connection is None or connection.status != "active":
            await update.message.reply_text(
                "Your WHOOP isn't connected yet (or needs reconnecting) — use /connect_whoop."
            )
            return

        try:
            # Pull at send time: WHOOP recovery often finalizes around wake-up
            await pull_and_store(session, user, days=7)
        except WhoopAuthError as exc:
            await send_admin_alert(f"WHOOP auth failed for user {user.id} during /today: {exc}")
            await update.message.reply_text(
                "Your WHOOP connection stopped working — please reconnect with /connect_whoop."
            )
            return
        except Exception as exc:
            logger.exception("/today pull failed for user %s", user.id)
            await send_admin_alert(f"/today pull failed for user {user.id}: {exc}")
            await update.message.reply_text(
                "I couldn't pull your latest WHOOP data just now — I've flagged it and will look into it."
            )
            return

        target_date = _today_local(user)
        snapshot = build_daily_snapshot(session, user.id, target_date)
        payload = build_daily_payload(user, snapshot)

        try:
            message_text = await generate_daily_message(payload)
        except Exception as exc:
            logger.exception("/today AI call failed for user %s", user.id)
            await send_admin_alert(f"/today AI call failed for user {user.id}: {exc}")
            await update.message.reply_text(
                "I couldn't write today's message — I've flagged it and will look into it."
            )
            return

        caution = safety_message(snapshot.safety_triggers)
        if caution:
            message_text = f"{message_text}\n\n{caution}"

        session.add(
            CoachMessage(
                user_id=user.id,
                date=target_date,
                message_type="daily",
                summary_payload=payload,
                ai_response=message_text,
            )
        )
        session.commit()

    await update.message.reply_text(message_text)


async def plain_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text(
        "I can't answer questions quite yet — that's coming soon. For now, /today gets you your coach message."
    )


def _today_local(user: User) -> date:
    return datetime.now(ZoneInfo(user.timezone)).date()
