"""Scheduled morning message job — wake-aware.

Instead of firing at a fixed clock hour, the job polls across a local morning
window (MORNING_WATCH_START_LOCAL .. MORNING_WATCH_CUTOFF_LOCAL) and sends the
message once WHOOP shows the user's main (non-nap) sleep has ended and
sleep/recovery data is usable. If the data is still not ready by the cutoff, a
degraded fallback is sent. At most one message per local date (idempotency via
coach_messages). The /today manual command is a separate, unconditional path and
is unaffected by this watcher.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

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
    filter_fresh_triggers,
    get_checkin_streak,
    get_previous_daily_message,
    safety_message,
    should_gap_fill,
)
from app.models.daily_metric import DailyMetric
from app.services.coach_payload_builder import build_daily_payload
from app.services.timekit import get_user_now, get_user_today
from app.services.whoop_client import WhoopAuthError
from app.services.withings_client import WithingsAuthError, WithingsRateLimitError
from app.telegram.bot import get_application
from app.telegram.keyboards import checkin_keyboard

logger = logging.getLogger(__name__)

# After the cutoff we still allow the degraded fallback (or a late "ready" send)
# for this many minutes, then give up for the day. Bounds the watch so a
# late-starting app can't fire a "morning" message in the evening.
_FALLBACK_GRACE_MINUTES = 180

# Guards against one tick's send being re-entered by an overlapping tick.
_in_flight: set[int] = set()


def morning_cron_minute_spec(interval_minutes: int) -> str:
    """Cron `minute` spec that fires every `interval_minutes` (e.g. 30 -> "0,30").

    Clamped to [1, 60] so a bad config can't break the scheduler.
    """
    interval = max(1, min(60, interval_minutes))
    return ",".join(str(m) for m in range(0, 60, interval))


def _local_minutes(user: User) -> int:
    now = get_user_now(user)
    return now.hour * 60 + now.minute


def _sleep_usable(row: DailyMetric | None) -> bool:
    """True when today's row has usable main-sleep data.

    metrics_normalizer maps only the SCORED, non-nap primary sleep to the local
    waking date, so a row that carries recovery or sleep hours means the main
    sleep ended and is usable. Naps never create a row, so they can't trigger a
    send. A missing row, or a row with neither metric, means "still pending".
    """
    return row is not None and (row.recovery_score is not None or row.sleep_hours is not None)


def decide_morning_action(
    now_minutes: int,
    start_minutes: int,
    cutoff_minutes: int,
    *,
    ready: bool,
    already_sent: bool,
    grace_minutes: int = _FALLBACK_GRACE_MINUTES,
) -> str:
    """Pure decision for one tick. Returns one of:

    - "skip"          already sent, too early, or past the whole window
    - "wait"          in window, main sleep not ready yet — try again next tick
    - "send_full"     main sleep ended + usable — send the real message
    - "send_degraded" past cutoff and still not ready — send the fallback

    A "ready" user sends whenever their sleep finalizes, even past the cutoff
    (someone who sleeps until 11 still gets a real morning message), up to the
    grace ceiling. The cutoff only governs the degraded fallback.
    """
    if already_sent:
        return "skip"
    if now_minutes < start_minutes:
        return "skip"
    if now_minutes > cutoff_minutes + grace_minutes:
        return "skip"
    if ready:
        return "send_full"
    if now_minutes >= cutoff_minutes:
        return "send_degraded"
    return "wait"


def has_sent_morning_message(session, user_id: int, local_date: date) -> bool:
    """Whether today's daily message already went out (idempotency, no new table)."""
    return session.scalar(
        select(CoachMessage.id).where(
            CoachMessage.user_id == user_id,
            CoachMessage.message_type == "daily",
            CoachMessage.date == local_date,
        )
    ) is not None


async def run_daily_message() -> None:
    """Poll tick: for each active WHOOP user inside their LOCAL morning watch
    window, check wake-readiness and send when ready (or a degraded fallback at
    the cutoff). Per-user timezone/DST via timekit; once-per-day idempotency in
    _send_for_user."""
    start_m = settings.morning_watch_start_minutes
    ceiling_m = settings.morning_watch_cutoff_minutes + _FALLBACK_GRACE_MINUTES
    with SessionLocal() as session:
        users = session.scalars(
            select(User)
            .join(OAuthConnection, OAuthConnection.user_id == User.id)
            .where(OAuthConnection.provider == "whoop", OAuthConnection.status == "active")
        ).all()
        # Gate on each user's own local clock, never the server's. Only users
        # currently inside [start, cutoff+grace] are worth a readiness check.
        due_ids = [u.id for u in users if start_m <= _local_minutes(u) <= ceiling_m]

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
    # Idempotency: if today's daily message already went out, do nothing. Only the
    # first qualifying tick of the day sends; later ticks short-circuit here.
    with SessionLocal() as session:
        user = session.get(User, user_id)
        if user is None:
            return
        if has_sent_morning_message(session, user_id, get_user_today(user)):
            return

    _in_flight.add(user_id)
    try:
        await _do_send_for_user(user_id)
    finally:
        _in_flight.discard(user_id)


async def _do_send_for_user(user_id: int) -> None:
    from app.routes.withings_oauth import pull_withings_and_store

    bot = get_application().bot

    # Wake-aware: one fresh pull per tick, then decide. The polling cadence (the
    # scheduler re-running every MORNING_WATCH_INTERVAL_MINUTES) IS the retry —
    # we re-pull each tick until the main sleep is usable or the cutoff forces a
    # degraded send. No blocking sleep loop, so _in_flight is held only briefly.
    with SessionLocal() as session:
        user = session.get(User, user_id)
        if user is None:
            return
        try:
            await pull_and_store(session, user, days=7)
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
        today_row = session.scalar(
            select(DailyMetric).where(
                DailyMetric.user_id == user_id, DailyMetric.date == target_date
            )
        )
        ready = _sleep_usable(today_row)
        now_minutes = _local_minutes(user)

    action = decide_morning_action(
        now_minutes,
        settings.morning_watch_start_minutes,
        settings.morning_watch_cutoff_minutes,
        ready=ready,
        already_sent=False,
    )
    if action in ("skip", "wait"):
        logger.info(
            "Morning watch: user %s not sending yet (action=%s, ready=%s, now=%dmin)",
            user_id, action, ready, now_minutes,
        )
        return
    if action == "send_degraded":
        logger.info(
            "Morning watch: user %s past cutoff without usable sleep — sending degraded message",
            user_id,
        )

    # Withings pull is non-fatal — missing body comp is fine, WHOOP still runs
    withings_telegram_id: int | None = None
    try:
        with SessionLocal() as _s:
            _u = _s.get(User, user_id)
            if _u:
                withings_telegram_id = _u.telegram_id
        await pull_withings_and_store(user_id, days=7)
    except WithingsRateLimitError as exc:
        # Another request (e.g. an incoming webhook) already refreshed the token.
        # The pull will be retried next time; WHOOP data still drives the message.
        logger.info("Withings rate-limited for user %s, skipping pull: %s", user_id, exc)
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
        gap_fill = should_gap_fill(session, user.id, target_date, snapshot)
        from app.services.commitment_engine import get_active_commitments
        commitments = get_active_commitments(session, user.id, target_date)
        _notes = user.coach_notes if isinstance(user.coach_notes, dict) else {}
        from app.services import memory_retriever, recommendation_checkpoint, recommendation_ledger
        structured_memories = memory_retriever.for_morning(session, user.id)
        # Evaluate any due recommendation checkpoints first so today's message can
        # close the loop. Crash-proof — never block the morning message.
        try:
            recommendation_checkpoint.evaluate_due(session, user.id, target_date)
        except Exception:
            logger.exception("Recommendation checkpoint eval failed for user %s", user_id)
        rec_context = recommendation_ledger.build_context(session, user.id, target_date, limit=5)
        payload = build_daily_payload(
            user, snapshot, yesterday_tags=yesterday_tags,
            today_metric_row=today_row, checkin_streak=streak, now=now,
            previous_message=previous_message, closed_loops=closed_loops,
            gap_fill_question=gap_fill, commitments=commitments or None,
            coach_notes=_notes or None,
            structured_memories=structured_memories or None,
            recommendation_context=rec_context or None,
        )

        try:
            message_text = await generate_daily_message(payload, user_id=user_id)
        except Exception as exc:
            logger.exception("Morning AI call failed for user %s", user_id)
            await send_admin_alert(f"Morning AI call failed for user {user_id}: {exc}")
            return

        coach_notes = user.coach_notes if isinstance(user.coach_notes, dict) else {}
        fresh_triggers = filter_fresh_triggers(session, user.id, target_date, snapshot.safety_triggers, coach_notes=coach_notes)
        caution = safety_message(fresh_triggers)
        if caution:
            message_text = f"{message_text}\n\n{caution}"

        coach_message = CoachMessage(
            user_id=user.id,
            date=target_date,
            message_type="daily",
            summary_payload=payload,
            ai_response=message_text,
        )
        session.add(coach_message)
        session.commit()
        coach_message_id = coach_message.id
        telegram_id = user.telegram_id

    await bot.send_message(chat_id=telegram_id, text=message_text)
    log_outgoing(telegram_id, message_text, "ai_daily", user_id=user_id)
    await bot.send_message(
        chat_id=telegram_id,
        text="How was yesterday? Tap any that apply:",
        reply_markup=checkin_keyboard(set()),
    )

    # Background: extract checkable recommendations from the message we just sent.
    from app.services import recommendation_extractor
    asyncio.create_task(
        recommendation_extractor.run_for_message(
            user_id, message_text, source_type="daily", source_message_id=coach_message_id
        )
    )
