"""Scheduled morning message job.

Pulls fresh WHOOP data (and Withings body comp if connected), generates today's
coach message, sends it via Telegram, then prompts the daily check-in.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.jobs.daily_pull import pull_and_store
from app.models.coach_message import CoachMessage
from app.models.journal_entry import JournalEntry
from app.models.oauth_connection import OAuthConnection
from app.models.user import User
from app.services.ai_client import generate_daily_message
from app.services.alerts import send_admin_alert
from app.services.chat_logger import log_outgoing
from app.services.event_engine import enrich_closed_loops_with_meal_gap
from app.services.observation_engine import build_closed_loops, recalculate_observations
from app.services.baseline_engine import (
    build_daily_snapshot,
    get_checkin_streak,
    get_previous_daily_message,
    safety_message,
)
from app.models.daily_metric import DailyMetric
from app.services.coach_payload_builder import build_daily_payload
from app.services.timekit import get_user_now, get_user_today
from app.services.whoop_client import WhoopAuthError
from app.services.withings_client import WithingsAuthError
from app.telegram.bot import get_application
from app.telegram.keyboards import checkin_keyboard

logger = logging.getLogger(__name__)

# How many hours after the configured send-hour we'll still send (covers a
# missed/late tick). Idempotency below guarantees at most one send per day.
_SEND_WINDOW_HOURS = 3

# Guards against a still-running retry loop being re-entered by a later tick.
_in_flight: set[int] = set()


async def run_daily_message() -> None:
    """Hourly tick: send the morning message to each active user whose LOCAL
    send-time window has arrived. Per-user timezone (and DST) handled by
    timekit; once-per-day idempotency handled in _send_for_user."""
    send_hour = settings.daily_pull_hour
    with SessionLocal() as session:
        users = session.scalars(
            select(User)
            .join(OAuthConnection, OAuthConnection.user_id == User.id)
            .where(OAuthConnection.provider == "whoop", OAuthConnection.status == "active")
        ).all()
        # Gate on each user's own local clock, never the server's.
        due_ids = [
            u.id for u in users
            if send_hour <= get_user_now(u).hour < send_hour + _SEND_WINDOW_HOURS
        ]

    if not due_ids:
        return

    results = await asyncio.gather(*[_send_for_user(uid) for uid in due_ids], return_exceptions=True)
    for uid, result in zip(due_ids, results):
        if isinstance(result, Exception):
            logger.exception("Morning message unhandled exception for user %s", uid, exc_info=result)
            await send_admin_alert(f"Morning message unhandled exception for user {uid}: {result}")


async def _send_for_user(user_id: int) -> None:
    if user_id in _in_flight:
        return
    # Idempotency: if today's daily message already went out, do nothing. This is
    # what makes the hourly send-window safe — only the first qualifying tick sends.
    with SessionLocal() as session:
        user = session.get(User, user_id)
        if user is None:
            return
        already_sent = session.scalar(
            select(CoachMessage.id).where(
                CoachMessage.user_id == user_id,
                CoachMessage.message_type == "daily",
                CoachMessage.date == get_user_today(user),
            )
        )
    if already_sent:
        return

    _in_flight.add(user_id)
    try:
        await _do_send_for_user(user_id)
    finally:
        _in_flight.discard(user_id)


async def _do_send_for_user(user_id: int) -> None:
    from app.routes.withings_oauth import pull_withings_and_store

    bot = get_application().bot

    # Pull-at-send with retry: SPEC says retry every 30 min (max 4) if today's recovery
    # hasn't finalized yet (WHOOP recovery typically scores around wake time)
    for attempt in range(5):
        if attempt > 0:
            logger.info(
                "Recovery not yet available for user %s — retry %d/4 in 30 min", user_id, attempt
            )
            await asyncio.sleep(30 * 60)

        with SessionLocal() as session:
            user = session.get(User, user_id)
            if user is None:
                return
            try:
                await pull_and_store(session, user, days=7 if attempt == 0 else 2)
                if attempt == 0:
                    recalculate_observations(session, user_id)
            except WhoopAuthError as exc:
                await send_admin_alert(
                    f"WHOOP auth expired for user {user_id} during morning pull: {exc}"
                )
                await bot.send_message(
                    chat_id=user.telegram_id,
                    text="Your WHOOP connection stopped working — tap /connect_whoop to reconnect.",
                )
                return
            except Exception as exc:
                logger.exception("Morning pull failed for user %s", user_id)
                await send_admin_alert(f"Morning pull failed for user {user_id}: {exc}")
                return

            target_date = get_user_today(user)
            has_recovery = bool(
                session.scalar(
                    select(DailyMetric.recovery_score).where(
                        DailyMetric.user_id == user_id,
                        DailyMetric.date == target_date,
                        DailyMetric.recovery_score.is_not(None),
                    )
                )
            )

        if has_recovery or attempt == 4:
            break

    # Withings pull is non-fatal — missing body comp is fine, WHOOP still runs
    withings_telegram_id: int | None = None
    try:
        with SessionLocal() as _s:
            _u = _s.get(User, user_id)
            if _u:
                withings_telegram_id = _u.telegram_id
        await pull_withings_and_store(user_id, days=7)
    except WithingsAuthError as exc:
        logger.warning("Withings auth broken for user %s: %s", user_id, exc)
        await send_admin_alert(f"Withings auth broken for user {user_id}: {exc}")
        if withings_telegram_id:
            await bot.send_message(
                chat_id=withings_telegram_id,
                text="Your Withings scale disconnected — tap /connect_withings to reconnect.",
            )
    except Exception as exc:
        logger.warning("Withings pull skipped for user %s: %s", user_id, exc)

    # Build and send the message with whatever data is available
    with SessionLocal() as session:
        user = session.get(User, user_id)
        if user is None:
            return

        now = get_user_now(user)
        target_date = now.date()
        yesterday = target_date - timedelta(days=1)
        snapshot = build_daily_snapshot(session, user.id, target_date)

        yesterday_entry = session.scalar(
            select(JournalEntry).where(
                JournalEntry.user_id == user.id,
                JournalEntry.date == yesterday,
            )
        )
        yesterday_tags = list(yesterday_entry.tags or []) if yesterday_entry else []
        today_row = session.scalar(
            select(DailyMetric).where(
                DailyMetric.user_id == user.id, DailyMetric.date == target_date
            )
        )
        streak = get_checkin_streak(session, user.id, target_date)
        previous_message = get_previous_daily_message(session, user.id, target_date)
        closed_loops = build_closed_loops(session, user.id, target_date, yesterday_tags)
        enrich_closed_loops_with_meal_gap(session, user.id, target_date, closed_loops, today_row)
        payload = build_daily_payload(
            user, snapshot, yesterday_tags=yesterday_tags,
            today_metric_row=today_row, checkin_streak=streak, now=now,
            previous_message=previous_message, closed_loops=closed_loops,
        )

        try:
            message_text = await generate_daily_message(payload)
        except Exception as exc:
            logger.exception("Morning AI call failed for user %s", user_id)
            await send_admin_alert(f"Morning AI call failed for user {user_id}: {exc}")
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
        telegram_id = user.telegram_id

    await bot.send_message(chat_id=telegram_id, text=message_text)
    log_outgoing(telegram_id, message_text, "ai_daily", user_id=user_id)
    await bot.send_message(
        chat_id=telegram_id,
        text="How was yesterday? Tap any that apply:",
        reply_markup=checkin_keyboard(set()),
    )
