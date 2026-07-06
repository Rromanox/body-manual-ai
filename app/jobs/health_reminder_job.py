"""Recurring health-reminder job (retatrutide shot).

Hourly tick, gated on each user's LOCAL clock — mirrors supplement_reminder.
Sends a short due-date nudge once per due date (idempotency via
last_reminded_date). Never sends dosage or medical guidance.
"""
from __future__ import annotations

import logging

from app.db import SessionLocal
from app.models.user import User
from app.services import health_reminder
from app.services.alerts import send_admin_alert
from app.services.chat_logger import log_outgoing
from app.services.timekit import get_user_now, get_user_today
from app.telegram.bot import get_application
from app.telegram.keyboards import reta_confirm_keyboard

logger = logging.getLogger(__name__)

# Only nudge during waking hours; once-per-day is guaranteed by last_reminded_date.
REMIND_START_HOUR = 9
REMIND_END_HOUR = 21


async def run_health_reminders() -> None:
    with SessionLocal() as session:
        # Independent of WHOOP status — a med reminder must fire even if the
        # fitness connection is broken (Fix #3).
        users = health_reminder.users_with_active_reminders(session)
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
        to_send = [(r.name, r.reminder_type) for r in due]
        for r in due:
            health_reminder.mark_reminded(session, r.id, today, commit=False)
        if due:
            session.commit()
        telegram_id = user.telegram_id

    if not to_send:
        return
    bot = get_application().bot
    for name, reminder_type in to_send:
        text = f"{name} shot is due today."
        # One-tap confirm for the retatrutide shot so the schedule actually
        # advances (Bug #1) — without it, the reminder re-fires daily.
        markup = reta_confirm_keyboard() if reminder_type == health_reminder.RETA_TYPE else None
        await bot.send_message(chat_id=telegram_id, text=text, reply_markup=markup)
        log_outgoing(telegram_id, text, "system", user_id=user_id)
        logger.info("Sent health reminder to user %s: %s", user_id, name)
