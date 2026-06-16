from __future__ import annotations

import logging
from datetime import date, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import delete as sql_delete, select
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

import asyncio

from app.config import settings
from app.db import SessionLocal
from app.jobs.daily_pull import pull_and_store
from app.models.coach_message import CoachMessage
from app.models.journal_entry import JournalEntry
from app.models.oauth_connection import OAuthConnection
from app.models.observation import Observation
from app.models.user import User
from app.services.ai_client import generate_daily_message, generate_focus_response, generate_qa_response, generate_weekly_message
from app.services.alerts import send_admin_alert
from app.services.baseline_engine import (
    build_daily_snapshot,
    build_qa_context,
    build_weekly_snapshot,
    get_checkin_streak,
    get_previous_daily_message,
    safety_message,
)
from app.services.coach_payload_builder import build_daily_payload, build_qa_payload, build_weekly_payload
from app.services.chat_logger import log_outgoing
from app.services.observation_engine import build_closed_loops, recalculate_observations
from app.services.timekit import get_user_now, get_user_today, now_block
from app.services.experiment_engine import (
    end_experiment,
    format_experiment_text,
    get_experiment_summaries,
    infer_metrics,
    METRIC_META,
    start_experiment,
)
from app.services.whoop_client import WhoopAuthError, build_authorize_url, make_oauth_state
from app.services.withings_client import build_authorize_url as withings_authorize_url
from app.telegram.keyboards import checkin_keyboard, confirm_delete_keyboard, goal_keyboard

logger = logging.getLogger(__name__)

# SPEC §Safety Rules: symptom keywords detected in Q&A → hard-coded response, never AI
_MEDICAL_KEYWORDS = [
    "chest pain", "chest tightness", "heart attack", "can't breathe", "cannot breathe",
    "shortness of breath", "trouble breathing", "fainting", "fainted", "passed out",
    "blacked out", "severe dizziness", "severe headache", "sudden headache",
    "blurred vision", "vision problems", "numbness", "arm pain", "jaw pain",
    "heart palpitations", "racing heart", "irregular heartbeat", "skipping beats",
]

_MEDICAL_RESPONSE = (
    "What you're describing sounds like it could be a medical issue — please stop "
    "and contact a doctor or emergency services. Don't wait on me for this one."
)


def _has_medical_keyword(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in _MEDICAL_KEYWORDS)


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


async def connect_withings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.telegram_id == update.effective_user.id))
    if user is None:
        await update.message.reply_text("Run /start first so I can set you up.")
        return
    url = withings_authorize_url(make_oauth_state(update.effective_user.id))
    await update.message.reply_text(f"Tap to connect your Withings scale:\n\n{url}")


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
                "I couldn't pull your latest WHOOP data just now — I've flagged it."
            )
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
        from app.models.daily_metric import DailyMetric
        today_row = session.scalar(
            select(DailyMetric).where(
                DailyMetric.user_id == user.id, DailyMetric.date == target_date
            )
        )
        streak = get_checkin_streak(session, user.id, target_date)
        previous_message = get_previous_daily_message(session, user.id, target_date)
        closed_loops = build_closed_loops(session, user.id, target_date, yesterday_tags)
        payload = build_daily_payload(
            user, snapshot, yesterday_tags=yesterday_tags,
            today_metric_row=today_row, checkin_streak=streak, now=now,
            previous_message=previous_message, closed_loops=closed_loops,
        )

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
    log_outgoing(telegram_id, message_text, "ai_daily")


async def checkin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    telegram_id = update.effective_user.id
    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            await update.message.reply_text("Run /start first so I can set you up.")
            return
        yesterday = _today_local(user) - timedelta(days=1)
        entry = session.scalar(
            select(JournalEntry).where(
                JournalEntry.user_id == user.id, JournalEntry.date == yesterday
            )
        )
        selected = set(entry.tags or []) if entry else set()

    await update.message.reply_text(
        f"How was yesterday ({yesterday.strftime('%A, %b %d')})? Tap any that apply:",
        reply_markup=checkin_keyboard(selected),
    )


async def checkin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.from_user is None:
        return
    await query.answer()

    data = query.data or ""
    telegram_id = query.from_user.id

    reply_text: str | None = None
    new_keyboard = None
    recalc_user_id: int | None = None

    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            return
        yesterday = _today_local(user) - timedelta(days=1)
        entry = session.scalar(
            select(JournalEntry).where(
                JournalEntry.user_id == user.id, JournalEntry.date == yesterday
            )
        )

        if data in ("ci_done", "ci_feel_skip"):
            tags = list(entry.tags or []) if entry else []
            reply_text = f"Saved ✓ — {', '.join(tags) if tags else 'nothing notable yesterday'}."
            recalc_user_id = user.id

        elif data == "ci_none":
            if entry is None:
                entry = JournalEntry(user_id=user.id, date=yesterday, tags=[])
                session.add(entry)
            entry.tags = []
            session.commit()
            reply_text = "Saved ✓ — nothing notable yesterday."
            recalc_user_id = user.id

        elif data.startswith("ci_feel:"):
            feel = int(data.split(":")[1])
            if entry is None:
                entry = JournalEntry(user_id=user.id, date=yesterday, tags=[])
                session.add(entry)
            entry.feel_score = feel
            session.commit()
            reply_text = f"Saved ✓ — feel score {feel}/5."

        elif data.startswith("ci_tag:"):
            tag = data.split(":")[1]
            if entry is None:
                entry = JournalEntry(user_id=user.id, date=yesterday, tags=[])
                session.add(entry)
            current = list(entry.tags or [])
            if tag in current:
                current.remove(tag)
            else:
                current.append(tag)
            entry.tags = current
            session.commit()
            new_keyboard = checkin_keyboard(set(current))

    if reply_text:
        await query.edit_message_text(reply_text)
    elif new_keyboard:
        await query.edit_message_reply_markup(reply_markup=new_keyboard)

    if recalc_user_id:
        asyncio.create_task(_run_observation_recalc(recalc_user_id))


async def weekly(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    telegram_id = update.effective_user.id
    await context.bot.send_chat_action(chat_id=update.message.chat_id, action=ChatAction.TYPING)

    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            await update.message.reply_text("Run /start first so I can set you up.")
            return
        if not _has_active_whoop(session, user.id):
            await update.message.reply_text("Connect your WHOOP first with /connect_whoop.")
            return

        now = get_user_now(user)
        target_date = now.date()
        snapshot = build_weekly_snapshot(session, user.id, target_date)
        payload = build_weekly_payload(user, snapshot, now=now)

        try:
            message_text = await generate_weekly_message(payload)
        except Exception as exc:
            logger.exception("/weekly AI call failed for user %s", user.id)
            await send_admin_alert(f"/weekly AI call failed for user {user.id}: {exc}")
            await update.message.reply_text("I couldn't generate the weekly summary — I've flagged it.")
            return

        session.add(CoachMessage(
            user_id=user.id,
            date=target_date,
            message_type="weekly",
            summary_payload=payload,
            ai_response=message_text,
        ))
        session.commit()

    await update.message.reply_text(message_text)
    log_outgoing(telegram_id, message_text, "ai_weekly")


async def goal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.telegram_id == update.effective_user.id))
        if user is None:
            await update.message.reply_text("Run /start first so I can set you up.")
            return
        current = user.goal or "general_health"
    labels = {"general_health": "General health", "performance": "Performance", "weight_loss": "Weight loss"}
    await update.message.reply_text(
        f"Current goal: {labels.get(current, current)}\n\nChoose a new goal:",
        reply_markup=goal_keyboard(current),
    )


async def goal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.from_user is None:
        return
    await query.answer()
    new_goal = (query.data or "").replace("goal:", "")
    if new_goal not in ("general_health", "performance", "weight_loss"):
        return
    labels = {"general_health": "General health", "performance": "Performance", "weight_loss": "Weight loss"}
    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.telegram_id == query.from_user.id))
        if user is None:
            return
        user.goal = new_goal
        session.commit()
    await query.edit_message_text(f"Goal updated to: {labels[new_goal]}. Your morning messages will reflect this.")


async def timezone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """View or set the user's IANA timezone. /timezone America/New_York"""
    if update.message is None or update.effective_user is None:
        return
    telegram_id = update.effective_user.id
    arg = (context.args or [""])[0].strip()

    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            await update.message.reply_text("Run /start first so I can set you up.")
            return

        if not arg:
            now = get_user_now(user)
            await update.message.reply_text(
                f"Your timezone is {user.timezone}.\n"
                f"Local time right now: {now.strftime('%A')} {now.strftime('%I:%M %p').lstrip('0')}.\n\n"
                "To change it, send an IANA name, e.g.\n/timezone America/New_York"
            )
            return

        try:
            ZoneInfo(arg)
        except (ZoneInfoNotFoundError, ValueError):
            await update.message.reply_text(
                f"'{arg}' isn't a valid timezone name. Use an IANA name like "
                "America/New_York, America/Chicago, or Europe/London."
            )
            return

        user.timezone = arg
        session.commit()
        now = get_user_now(user)
    await update.message.reply_text(
        f"Timezone set to {arg}. Local time now: {now.strftime('%A')} "
        f"{now.strftime('%I:%M %p').lstrip('0')}."
    )


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.telegram_id == update.effective_user.id))
        if user is None:
            await update.message.reply_text("Run /start first so I can set you up.")
            return
        rows = session.scalars(
            select(CoachMessage)
            .where(CoachMessage.user_id == user.id, CoachMessage.message_type == "daily")
            .order_by(CoachMessage.date.desc())
            .limit(7)
        ).all()

    if not rows:
        await update.message.reply_text("No daily messages yet — run /today to get your first one.")
        return

    parts = []
    for row in rows:
        date_str = row.date.strftime("%a, %b %d") if row.date else "Unknown date"
        parts.append(f"*{date_str}*\n{row.ai_response}")
    await update.message.reply_text("\n\n---\n\n".join(parts), parse_mode="Markdown")


async def focus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    telegram_id = update.effective_user.id
    await context.bot.send_chat_action(chat_id=update.message.chat_id, action=ChatAction.TYPING)

    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            await update.message.reply_text("Run /start first so I can set you up.")
            return
        if not _has_active_whoop(session, user.id):
            await update.message.reply_text("Connect your WHOOP first with /connect_whoop.")
            return
        now = get_user_now(user)
        target_date = now.date()
        snapshot = build_weekly_snapshot(session, user.id, target_date)
        payload = build_weekly_payload(user, snapshot, now=now)

    try:
        text = await generate_focus_response(payload)
    except Exception as exc:
        logger.exception("/focus AI call failed for user %s", telegram_id)
        await send_admin_alert(f"/focus AI call failed for user {telegram_id}: {exc}")
        await update.message.reply_text("Couldn't generate a focus right now — try again in a moment.")
        return

    await update.message.reply_text(text)
    log_outgoing(telegram_id, text, "ai_focus")


async def manual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    telegram_id = update.effective_user.id

    from app.models.daily_metric import DailyMetric as _DM
    from sqlalchemy import func as _func

    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            await update.message.reply_text("Run /start first so I can set you up.")
            return

        # Recalculate observations so the manual always shows fresh counts
        try:
            recalculate_observations(session, user.id)
        except Exception:
            logger.exception("Observation recalc failed on /manual for user %s", user.id)

        observations = session.scalars(
            select(Observation)
            .where(Observation.user_id == user.id, Observation.status != "archived")
            .order_by(Observation.occurrence_count.desc())
        ).all()
        name = user.first_name or "Your"

        # Baselines — always shown
        target_date = _today_local(user)
        month_start = target_date - timedelta(days=30)

        def _avg30(col):
            return session.scalar(
                select(_func.avg(col)).where(
                    _DM.user_id == user.id,
                    _DM.date >= month_start,
                    _DM.date < target_date,
                    col.is_not(None),
                )
            )

        avg_recovery = _avg30(_DM.recovery_score)
        avg_hrv = _avg30(_DM.hrv_ms)
        avg_sleep = _avg30(_DM.sleep_hours)
        avg_rhr = _avg30(_DM.resting_heart_rate)
        avg_weight = _avg30(_DM.weight)
        avg_bf = _avg30(_DM.body_fat_pct)

        exp_summaries = get_experiment_summaries(session, user.id, target_date)

    lines = [f"*{name}'s Body Manual*\n"]

    # Baselines block — always at the top
    lines.append("*Your 30-day baselines:*")
    has_any_baseline = False
    if avg_recovery:
        lines.append(f"Recovery {avg_recovery:.0f}  ·  HRV {avg_hrv:.0f}ms  ·  Sleep {avg_sleep:.1f}h  ·  RHR {avg_rhr:.0f}bpm")
        has_any_baseline = True
    if avg_weight:
        bf_str = f"  ·  Body fat {avg_bf:.1f}%" if avg_bf else ""
        lines.append(f"Weight {avg_weight * 2.20462:.1f} lbs{bf_str}")
        has_any_baseline = True
    if not has_any_baseline:
        lines.append("_No data yet — run /today after connecting WHOOP._")

    # Patterns block
    lines.append("")
    if not observations:
        lines.append("*Patterns:* _Still building — check in daily and patterns will appear here after a few weeks._")
    else:
        stronger = [o for o in observations if o.status == "stronger_signal"]
        promising = [o for o in observations if o.status == "promising"]
        watching = [o for o in observations if o.status in ("watching", "weak")]

        if stronger:
            lines.append("*Stronger Signals:*")
            for o in stronger:
                lines.append(f"• {o.pattern_description}\n  {o.supporting_count} of {o.occurrence_count} logged days.")
        if promising:
            lines.append("\n*Emerging Patterns:*")
            for o in promising:
                lines.append(f"• {o.pattern_description}\n  {o.supporting_count} of {o.occurrence_count} days. Early signal.")
        if watching:
            lines.append("\n*Watching:*")
            for o in watching:
                lines.append(f"• {o.pattern_description}\n  {o.supporting_count} of {o.occurrence_count} days so far.")

    # Experiments section
    if exp_summaries:
        lines.append("\n*Experiments*")
        for s in exp_summaries:
            lines.append(format_experiment_text(s))

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def experiment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /experiment               — show active experiments
    /experiment <name>        — start a new experiment
    /experiment end           — end the most recent active experiment
    /experiment end <partial> — end experiment whose name contains <partial>
    /experiment list          — list all experiments including completed
    """
    if update.message is None or update.effective_user is None:
        return
    telegram_id = update.effective_user.id
    args = context.args or []

    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            await update.message.reply_text("Run /start first so I can set you up.")
            return

        from app.models.experiment import Experiment as _Exp

        target_date = _today_local(user)

        # --- /experiment end [partial name] ---
        if args and args[0].lower() == "end":
            partial = " ".join(args[1:]).lower()
            active = session.scalars(
                select(_Exp).where(_Exp.user_id == user.id, _Exp.status == "active")
                .order_by(_Exp.start_date.desc())
            ).all()
            if not active:
                await update.message.reply_text("No active experiments to end.")
                return
            target_exp = next(
                (e for e in active if partial in e.name.lower()), active[0]
            ) if partial else active[0]
            end_experiment(session, target_exp, target_date)
            await update.message.reply_text(
                f"Ended *{target_exp.name}* after {(target_date - target_exp.start_date).days + 1} days.\n"
                "Results are in /manual under Experiments.",
                parse_mode="Markdown",
            )
            return

        # --- /experiment list ---
        if args and args[0].lower() == "list":
            summaries = get_experiment_summaries(session, user.id, target_date)
            if not summaries:
                await update.message.reply_text(
                    "No experiments yet. Start one with /experiment <name>, e.g.\n"
                    "/experiment Cutting alcohol"
                )
                return
            parts = [format_experiment_text(s) for s in summaries]
            await update.message.reply_text("\n\n".join(parts), parse_mode="Markdown")
            return

        # --- /experiment (no args) → show active ---
        if not args:
            active = get_experiment_summaries(session, user.id, target_date)
            active = [s for s in active if s["status"] == "active"]
            if not active:
                await update.message.reply_text(
                    "No active experiments.\n\n"
                    "Start one with /experiment <name>, e.g.:\n"
                    "• /experiment Cutting alcohol\n"
                    "• /experiment Earlier bedtime\n"
                    "• /experiment Intermittent fasting\n\n"
                    "I'll auto-select which metrics to track based on the name, "
                    "and compare them to your 14-day baseline."
                )
            else:
                parts = [format_experiment_text(s) for s in active]
                parts.append("\nEnd one with /experiment end")
                await update.message.reply_text("\n\n".join(parts), parse_mode="Markdown")
            return

        # --- /experiment <name> → start new ---
        name = " ".join(args)
        metrics = infer_metrics(name)
        metric_labels = [METRIC_META.get(m, (m, True))[0] for m in metrics]

        exp = start_experiment(session, user.id, name, target_date, metrics)
        await update.message.reply_text(
            f"Started *{name}* — Day 1.\n\n"
            f"Tracking: {', '.join(metric_labels)}\n"
            f"Baseline: your last 14 days before today.\n\n"
            "I'll compare your metrics from today forward. "
            "Check progress anytime with /experiment or in /manual.",
            parse_mode="Markdown",
        )


async def delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    await update.message.reply_text(
        "⚠️ This will permanently delete all your data — recovery metrics, sleep data, "
        "coach messages, journal entries, and WHOOP tokens. There is no undo.\n\nAre you sure?",
        reply_markup=confirm_delete_keyboard(),
    )


async def delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.from_user is None:
        return
    await query.answer()

    data = query.data or ""
    telegram_id = query.from_user.id

    if data == "del_cancel":
        await query.edit_message_text("Cancelled — your data is safe.")
        return

    if data == "del_confirm":
        with SessionLocal() as session:
            user = session.scalar(select(User).where(User.telegram_id == telegram_id))
            if user:
                session.execute(sql_delete(User).where(User.id == user.id))
                session.commit()
        await query.edit_message_text(
            "Done — all your data has been permanently deleted. "
            "Send /start if you ever want to begin again."
        )


async def backfill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    telegram_id = update.effective_user.id
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
            await update.message.reply_text("Your WHOOP isn't connected — use /connect_whoop first.")
            return
        user_id = user.id

    await update.message.reply_text("Pulling your last 365 days from WHOOP and Withings — this may take a minute…")
    try:
        with SessionLocal() as session:
            user = session.get(User, user_id)
            written = await pull_and_store(session, user, days=365)
    except WhoopAuthError as exc:
        await send_admin_alert(f"WHOOP auth failed for user {user_id} during /backfill: {exc}")
        await update.message.reply_text("Your WHOOP connection stopped working — reconnect with /connect_whoop.")
        return
    except Exception as exc:
        logger.exception("/backfill failed for user %s", user_id)
        await send_admin_alert(f"/backfill failed for user {user_id}: {exc}")
        msg = str(exc)
        if "rate limit" in msg.lower() or "429" in msg:
            await update.message.reply_text(
                "WHOOP rate-limited the request — you likely just did a large pull. "
                "Wait 5 minutes and try /backfill again."
            )
        else:
            await update.message.reply_text("Something went wrong pulling WHOOP data — I've flagged it.")
        return

    from app.routes.withings_oauth import pull_withings_and_store
    withings_written = 0
    try:
        withings_written = await pull_withings_and_store(user_id, days=365)
    except Exception as exc:
        logger.warning("/backfill Withings pull failed for user %s: %s", user_id, exc)

    parts = [f"WHOOP: {written} days"]
    if withings_written:
        parts.append(f"Withings: {withings_written} days")
    await update.message.reply_text(f"Done — {', '.join(parts)} loaded.")


async def plain_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Any non-command message is a question to the coach."""
    if update.message is None or update.effective_user is None:
        return
    question = (update.message.text or "").strip()
    if not question:
        return
    if _has_medical_keyword(question):
        await update.message.reply_text(_MEDICAL_RESPONSE)
        return
    telegram_id = update.effective_user.id
    await context.bot.send_chat_action(chat_id=update.message.chat_id, action=ChatAction.TYPING)

    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            await update.message.reply_text("Run /start first so I can set you up.")
            return
        if not _has_active_whoop(session, user.id):
            await update.message.reply_text(
                "I need your WHOOP data to answer questions — connect with /connect_whoop first."
            )
            return

        now = get_user_now(user)
        target_date = now.date()
        qa_ctx = build_qa_context(session, user.id, target_date, user=user)
        payload = build_qa_payload(question, qa_ctx, now=now_block(user, now))
        user_id = user.id

        # Last 5 Q&A exchanges as conversation history so the AI remembers the thread
        history_rows = session.scalars(
            select(CoachMessage)
            .where(
                CoachMessage.user_id == user.id,
                CoachMessage.message_type == "q_and_a",
                CoachMessage.ai_response != "",
            )
            .order_by(CoachMessage.id.desc())
            .limit(5)
        ).all()
        history: list[dict[str, str]] = []
        for row in reversed(history_rows):
            q = (row.summary_payload or {}).get("question", "")
            if q and row.ai_response:
                history.append({"role": "user", "content": q})
                history.append({"role": "assistant", "content": row.ai_response})

        session.add(CoachMessage(
            user_id=user.id,
            date=target_date,
            message_type="q_and_a",
            summary_payload=payload,
            ai_response="",
        ))
        session.commit()
        msg_id = session.scalars(
            select(CoachMessage.id).where(
                CoachMessage.user_id == user_id,
                CoachMessage.message_type == "q_and_a",
            ).order_by(CoachMessage.id.desc()).limit(1)
        ).first()

    try:
        response_text = await generate_qa_response(payload, history=history)
    except Exception as exc:
        logger.exception("Q&A call failed for user %s", user_id)
        await send_admin_alert(f"Q&A call failed for user {user_id}: {exc}")
        await update.message.reply_text("I hit a snag answering that — try again in a moment.")
        return

    if msg_id:
        with SessionLocal() as session:
            msg = session.get(CoachMessage, msg_id)
            if msg:
                msg.ai_response = response_text
                session.commit()

    await update.message.reply_text(response_text)
    log_outgoing(telegram_id, response_text, "q_and_a")


async def chatlog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show recent chat history (both directions) for debugging and review."""
    if update.message is None or update.effective_user is None:
        return
    telegram_id = update.effective_user.id

    # Optional arg: number of messages to show (default 30, max 100)
    try:
        limit = min(int((context.args or ["50"])[0]), 200)
    except (ValueError, IndexError):
        limit = 50

    from app.models.message_log import MessageLog as _ML
    try:
        with SessionLocal() as session:
            rows = session.scalars(
                select(_ML)
                .where(_ML.telegram_id == telegram_id)
                .order_by(_ML.created_at.desc())
                .limit(limit)
            ).all()
    except Exception as exc:
        logger.exception("/chatlog DB query failed")
        await update.message.reply_text(
            f"Couldn't read chat log — the table may still be migrating. "
            f"Try again in a moment. (Error: {type(exc).__name__})"
        )
        return

    if not rows:
        await update.message.reply_text(
            "No chat history yet — the log started recording after the last deploy. "
            "Send any message or command and then /chatlog again."
        )
        return

    TYPE_EMOJI = {
        "command": "⌨️",
        "q_and_a": "💬",
        "ai_daily": "🌅",
        "ai_weekly": "📊",
        "ai_focus": "🎯",
        "checkin": "✅",
        "system": "⚙️",
        "error": "❌",
    }
    lines = [f"Chat log — last {min(limit, len(rows))} messages\n"]
    for row in reversed(rows):
        ts = row.created_at.strftime("%b %d %H:%M") if row.created_at else "?"
        icon = "👤" if row.direction == "in" else "🤖"
        type_tag = TYPE_EMOJI.get(row.message_type, "")
        lines.append(f"[{ts}] {icon}{type_tag} {row.content}")

    # No parse_mode — message content can contain Markdown special chars that break the parser
    async def _send(text: str) -> None:
        try:
            await update.message.reply_text(text)
        except Exception as exc:
            logger.exception("/chatlog send failed")
            await update.message.reply_text(f"Send error: {type(exc).__name__}: {exc}")

    # Split into chunks under Telegram's 4096 char limit
    chunk: list[str] = []
    for line in lines:
        if sum(len(l) + 1 for l in chunk) + len(line) > 3800:
            await _send("\n".join(chunk))
            chunk = []
        chunk.append(line)
    if chunk:
        await _send("\n".join(chunk))


async def _run_observation_recalc(user_id: int) -> None:
    try:
        with SessionLocal() as session:
            recalculate_observations(session, user_id)
    except Exception:
        logger.exception("Observation recalc failed for user %s", user_id)


def _today_local(user: User) -> date:
    # Thin wrapper kept for existing call sites; the clock lives in timekit.
    return get_user_today(user)


def _has_active_whoop(session, user_id: int) -> bool:
    conn = session.scalar(
        select(OAuthConnection).where(
            OAuthConnection.user_id == user_id,
            OAuthConnection.provider == "whoop",
            OAuthConnection.status == "active",
        )
    )
    return conn is not None
