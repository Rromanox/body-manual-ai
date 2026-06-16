"""Scheduled weekly summary job (SPEC §8: "Sent Sunday evening").

Mirrors daily_message.py's hourly-tick + per-user-local-clock pattern, with
one extra gate: the user's local day must be Sunday. Idempotency is the same
trick as the daily job — a CoachMessage already exists for this date/type.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models.coach_message import CoachMessage
from app.models.oauth_connection import OAuthConnection
from app.models.user import User
from app.services.ai_client import generate_weekly_message
from app.services.alerts import send_admin_alert
from app.services.baseline_engine import build_weekly_snapshot
from app.services.chat_logger import log_outgoing
from app.services.coach_payload_builder import build_weekly_payload
from app.services.timekit import get_user_now
from app.telegram.bot import get_application

logger = logging.getLogger(__name__)

_SUNDAY = 6  # date.weekday()
_SEND_WINDOW_HOURS = 3


async def run_weekly_message() -> None:
    send_hour = settings.weekly_send_hour
    with SessionLocal() as session:
        users = session.scalars(
            select(User)
            .join(OAuthConnection, OAuthConnection.user_id == User.id)
            .where(OAuthConnection.provider == "whoop", OAuthConnection.status == "active")
        ).all()
        due_ids = []
        for u in users:
            now = get_user_now(u)
            if now.weekday() == _SUNDAY and send_hour <= now.hour < send_hour + _SEND_WINDOW_HOURS:
                due_ids.append(u.id)

    for user_id in due_ids:
        try:
            await _send_for_user(user_id)
        except Exception as exc:
            logger.exception("Weekly message failed for user %s", user_id)
            await send_admin_alert(f"Weekly message failed for user {user_id}: {exc}")


async def _send_for_user(user_id: int) -> None:
    with SessionLocal() as session:
        user = session.get(User, user_id)
        if user is None:
            return

        now = get_user_now(user)
        target_date = now.date()

        # Idempotency: skip if question or full summary already sent today
        already_sent = session.scalar(
            select(CoachMessage.id).where(
                CoachMessage.user_id == user_id,
                CoachMessage.message_type.in_(["weekly", "weekly_question"]),
                CoachMessage.date == target_date,
            )
        )
        if already_sent:
            return

        snapshot = build_weekly_snapshot(session, user.id, target_date)
        payload = build_weekly_payload(user, snapshot, now=now)

        # Two-turn flow: send the question first, store the pre-built payload.
        # When the user replies, plain_text() detects the pending weekly_question
        # and generates the full summary incorporating their reflection.
        question = "How did this week feel overall — anything that stood out, good or bad?"
        session.add(CoachMessage(
            user_id=user.id,
            date=target_date,
            message_type="weekly_question",
            summary_payload=payload,
            ai_response=question,
        ))
        session.commit()
        telegram_id = user.telegram_id

    bot = get_application().bot
    await bot.send_message(chat_id=telegram_id, text=question)
    log_outgoing(telegram_id, question, "weekly_question", user_id=user_id)
