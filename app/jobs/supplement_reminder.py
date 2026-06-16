"""Creatine reminder job.

Fires up to twice a day per user, gated on their LOCAL clock: once around
noon, once around 9pm if they still haven't confirmed. Mirrors
daily_message.py's hourly-tick + per-user-timezone pattern, but idempotency
is tracked on the SupplementLog row's noon/evening flags rather than via a
single daily message.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from app.db import SessionLocal
from app.models.oauth_connection import OAuthConnection
from app.models.supplement_log import SupplementLog
from app.models.user import User
from app.services.alerts import send_admin_alert
from app.services.chat_logger import log_outgoing
from app.services.supplement_engine import SUPPLEMENT_NAME, get_today_log
from app.services.timekit import get_user_now
from app.telegram.bot import get_application
from app.telegram.keyboards import supplement_keyboard

logger = logging.getLogger(__name__)

NOON_HOUR = 12
EVENING_HOUR = 21
# Covers a missed/late tick, same idea as daily_message's _SEND_WINDOW_HOURS.
# Actual once-per-slot-per-day guarantee comes from the reminder-sent flags.
_WINDOW_HOURS = 3


async def run_supplement_reminder() -> None:
    with SessionLocal() as session:
        users = session.scalars(
            select(User)
            .join(OAuthConnection, OAuthConnection.user_id == User.id)
            .where(OAuthConnection.provider == "whoop", OAuthConnection.status == "active")
        ).all()
        due: list[tuple[int, str]] = []
        for u in users:
            hour = get_user_now(u).hour
            if NOON_HOUR <= hour < NOON_HOUR + _WINDOW_HOURS:
                due.append((u.id, "noon"))
            elif EVENING_HOUR <= hour < EVENING_HOUR + _WINDOW_HOURS:
                due.append((u.id, "evening"))

    for user_id, slot in due:
        try:
            await _maybe_send(user_id, slot)
        except Exception as exc:
            logger.exception("Supplement reminder failed for user %s (%s)", user_id, slot)
            await send_admin_alert(f"Supplement reminder failed for user {user_id} ({slot}): {exc}")


async def _maybe_send(user_id: int, slot: str) -> None:
    flag_col = "noon_reminder_sent" if slot == "noon" else "evening_reminder_sent"

    with SessionLocal() as session:
        user = session.get(User, user_id)
        if user is None:
            return
        target_date = get_user_now(user).date()
        log = get_today_log(session, user_id, target_date)

        if log and log.taken:
            return  # already confirmed today — nothing to nudge
        if log and getattr(log, flag_col):
            return  # this slot's reminder already went out today

        if log is None:
            log = SupplementLog(user_id=user_id, name=SUPPLEMENT_NAME, date=target_date)
            session.add(log)

        setattr(log, flag_col, True)
        session.commit()
        telegram_id = user.telegram_id

    text = (
        "Did you take your creatine today?"
        if slot == "noon"
        else "Quick check — still no creatine logged today. Taken it?"
    )
    bot = get_application().bot
    await bot.send_message(chat_id=telegram_id, text=text, reply_markup=supplement_keyboard())
    log_outgoing(telegram_id, text, "system", user_id=user_id)
