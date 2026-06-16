"""Proactive gated check-in (COACH_FEEL.md §The bar for unprompted messages).

Fires when recovery has been very low 3 days in a row. The backend computes
whether and when to fire; the message is a fixed template (no AI call).

Design constraints enforced here:
- At most one proactive message per day (idempotency via coach_messages).
- Only fires AFTER today's daily message has already gone out — avoids piling
  on before the morning message has even landed.
- Only fires on days 3–6 of a streak. Day 7+ is the safety trigger's job.
- Quiet hours: 9 AM – 6 PM only in the user's local timezone.
- If the user already logged something today, we already have context — skip.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select

from app.db import SessionLocal
from app.models.coach_message import CoachMessage
from app.models.daily_metric import DailyMetric
from app.models.event import Event
from app.models.journal_entry import JournalEntry
from app.models.oauth_connection import OAuthConnection
from app.models.user import User
from app.services.alerts import send_admin_alert
from app.services.baseline_engine import RECOVERY_VERY_LOW
from app.services.chat_logger import log_outgoing
from app.services.timekit import get_user_now, get_user_today
from app.telegram.bot import get_application

logger = logging.getLogger(__name__)

_STREAK_MIN = 3
_STREAK_MAX = 6  # 7+ is the safety-trigger window — handled separately
_QUIET_BEFORE = 9
_QUIET_AFTER = 18


async def run_proactive_check() -> None:
    """Hourly tick — send a gated proactive ping when conditions are met."""
    with SessionLocal() as session:
        users = session.scalars(
            select(User)
            .join(OAuthConnection, OAuthConnection.user_id == User.id)
            .where(OAuthConnection.provider == "whoop", OAuthConnection.status == "active")
        ).all()

    results = await asyncio.gather(*[_check_user(u) for u in users], return_exceptions=True)
    for u, result in zip(users, results):
        if isinstance(result, Exception):
            logger.exception("Proactive check failed for user %s", u.id, exc_info=result)
            await send_admin_alert(f"Proactive check failed for user {u.id}: {result}")


async def _check_user(user: User) -> None:
    bot = get_application().bot
    now = get_user_now(user)
    today = now.date()

    if now.hour < _QUIET_BEFORE or now.hour >= _QUIET_AFTER:
        return

    with SessionLocal() as session:
        # Only fire after the daily message has already gone out
        has_daily = bool(session.scalar(
            select(CoachMessage.id).where(
                CoachMessage.user_id == user.id,
                CoachMessage.message_type == "daily",
                CoachMessage.date == today,
            )
        ))
        if not has_daily:
            return

        # Idempotency: at most one proactive per day
        already_sent = bool(session.scalar(
            select(CoachMessage.id).where(
                CoachMessage.user_id == user.id,
                CoachMessage.message_type == "proactive",
                CoachMessage.date == today,
            )
        ))
        if already_sent:
            return

        # Skip if the user already logged something for today — we have context
        has_today_log = bool(session.scalar(
            select(Event.id).where(
                Event.user_id == user.id, Event.local_date == today
            )
        )) or bool(session.scalar(
            select(JournalEntry.id).where(
                JournalEntry.user_id == user.id, JournalEntry.date == today
            )
        ))
        if has_today_log:
            return

        streak = _low_recovery_streak(session, user.id, today)
        if streak < _STREAK_MIN or streak > _STREAK_MAX:
            return

        msg = (
            f"Recovery has been very low {streak} days in a row — anything going on? "
            "Logging what's happening (stress, travel, poor sleep) helps me see the pattern."
        )
        session.add(CoachMessage(
            user_id=user.id,
            date=today,
            message_type="proactive",
            summary_payload={"streak": streak, "trigger": "low_recovery"},
            ai_response=msg,
        ))
        session.commit()
        telegram_id = user.telegram_id

    await bot.send_message(chat_id=telegram_id, text=msg)
    log_outgoing(telegram_id, msg, "proactive", user_id=user.id)
    logger.info("Sent proactive ping to user %s (streak=%d)", user.id, streak)


def _low_recovery_streak(session, user_id: int, today) -> int:
    """Count consecutive days (ending today) where recovery_score < RECOVERY_VERY_LOW."""
    window_start = today - timedelta(days=10)
    rows = session.scalars(
        select(DailyMetric)
        .where(
            DailyMetric.user_id == user_id,
            DailyMetric.date >= window_start,
            DailyMetric.date <= today,
        )
        .order_by(DailyMetric.date.desc())
    ).all()
    by_date = {r.date: r for r in rows}
    streak = 0
    day = today
    while True:
        row = by_date.get(day)
        if row is None or row.recovery_score is None or row.recovery_score >= RECOVERY_VERY_LOW:
            break
        streak += 1
        day -= timedelta(days=1)
    return streak
