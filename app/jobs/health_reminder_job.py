"""Recurring health-reminder job (retatrutide shot).

Hourly tick, gated on each user's LOCAL clock — mirrors supplement_reminder.
Sends a short due-date nudge once per due date (idempotency via
last_reminded_date). Never sends dosage or medical guidance.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from app.db import SessionLocal
from app.models.oauth_connection import OAuthConnection
from app.models.user import User
from app.services import health_reminder
from app.services.alerts import send_admin_alert
from app.services.chat_logger import log_outgoing
from app.services.timekit import get_user_now, get_user_today
from app.telegram.bot import get_application

logger = logging.getLogger(__name__)

# Only nudge during waking hours; once-per-day is guaranteed by last_reminded_date.
REMIND_START_HOUR = 9
REMIND_END_HOUR = 21


async def run_health_reminders() -> None:
    with SessionLocal() as session:
        users = session.scalars(
            select(User)
            .join(OAuthConnection, OAuthConnection.user_id == User.id)
            .where(OAuthConnection.provider == "whoop", OAuthConnection.status == "active")
        ).all()
        due_ids = [u.id for u in users if REMIND_START_HOUR <= get_user_now(u).hour < REMIND_END_HOUR]

    for user_id in due_ids:
        try:
            await _maybe_remind(user_id)
        except Exception as exc:
            logger.exception("Health reminder failed for user %s", user_id)
            await send_admin_alert(f"Health reminder failed for user {user_id}: {exc}")


async def _maybe_remind(user_id: int) -> None:
    with SessionLocal() as session:
        user = session.get(User, user_id)
        if user is None:
            return
        today = get_user_today(user)
        due = health_reminder.due_reminders(session, user_id, today)
        names = [r.name for r in due]
        for r in due:
            health_reminder.mark_reminded(session, r.id, today, commit=False)
        if due:
            session.commit()
        telegram_id = user.telegram_id

    if not names:
        return
    bot = get_application().bot
    for name in names:
        text = f"{name} shot is due today."
        await bot.send_message(chat_id=telegram_id, text=text)
        log_outgoing(telegram_id, text, "system", user_id=user_id)
        logger.info("Sent health reminder to user %s: %s", user_id, name)
