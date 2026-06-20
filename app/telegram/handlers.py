from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
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
from app.models.event import Event
from app.models.journal_entry import JournalEntry
from app.models.oauth_connection import OAuthConnection
from app.models.observation import Observation
from app.models.user import User
from app.services.ai_client import classify_and_extract, extract_user_facts, generate_daily_message, generate_focus_response, generate_qa_response, generate_weekly_message
from app.services.alerts import send_admin_alert
from app.services.baseline_engine import (
    build_daily_snapshot,
    build_qa_context,
    build_weekly_snapshot,
    filter_fresh_triggers,
    get_checkin_streak,
    get_previous_daily_message,
    safety_message,
    should_gap_fill,
)
from app.services.coach_payload_builder import build_daily_payload, build_qa_payload, build_weekly_payload
from app.services import (
    health_reminder,
    memory_extractor,
    memory_retriever,
    memory_store,
    message_intent,
    output_guard,
    recommendation_checkpoint,
    recommendation_extractor,
    recommendation_followthrough,
    recommendation_ledger,
    weight_projection,
)
from app.services.chat_logger import log_outgoing
from app.services.event_engine import EVENT_TYPES, apply_event_to_tags, enrich_closed_loops_with_meal_gap
from app.services.observation_engine import POSITIVE_TAGS, build_closed_loops, recalculate_observations
from app.services.supplement_engine import get_today_log, mark_taken
from app.services.timekit import get_user_now, get_user_today, now_block, resolve_local_time
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
from app.telegram.keyboards import (
    checkin_keyboard,
    confirm_delete_keyboard,
    feel_keyboard,
    goal_keyboard,
    supplement_keyboard,
)

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
        enrich_closed_loops_with_meal_gap(session, user.id, target_date, closed_loops, today_row)
        gap_fill = should_gap_fill(session, user.id, target_date, snapshot)
        from app.services.commitment_engine import get_active_commitments
        commitments = get_active_commitments(session, user.id, target_date)
        _notes = user.coach_notes if isinstance(user.coach_notes, dict) else {}
        structured_memories = memory_retriever.for_morning(session, user.id)
        try:
            recommendation_checkpoint.evaluate_due(session, user.id, target_date)
        except Exception:
            logger.exception("Recommendation checkpoint eval failed for user %s", user.id)
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
            message_text = await generate_daily_message(payload, user_id=user.id)
        except Exception as exc:
            logger.exception("/today AI call failed for user %s", user.id)
            await send_admin_alert(f"/today AI call failed for user {user.id}: {exc}")
            await update.message.reply_text(
                "I couldn't write today's message — I've flagged it and will look into it."
            )
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
        user_id = user.id

    await update.message.reply_text(message_text)
    log_outgoing(telegram_id, message_text, "ai_daily")
    asyncio.create_task(
        recommendation_extractor.run_for_message(
            user_id, message_text, source_type="daily", source_message_id=coach_message_id
        )
    )


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
    show_feel_step = False
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

        if data == "ci_done":
            tags = list(entry.tags or []) if entry else []
            tag_str = ", ".join(tags) if tags else "nothing notable yesterday"
            reply_text = f"Saved ✓ — {tag_str}."
            show_feel_step = True

        elif data == "ci_none":
            if entry is None:
                entry = JournalEntry(user_id=user.id, date=yesterday, tags=[])
                session.add(entry)
            entry.tags = []
            session.commit()
            reply_text = "Saved ✓ — nothing notable yesterday."
            show_feel_step = True

        elif data == "ci_feel_skip":
            reply_text = "Got it ✓"
            recalc_user_id = user.id

        elif data.startswith("ci_feel:"):
            feel = int(data.split(":")[1])
            if entry is None:
                entry = JournalEntry(user_id=user.id, date=yesterday, tags=[])
                session.add(entry)
            entry.feel_score = feel
            session.commit()
            reply_text = f"Feel score {feel}/5 saved ✓"
            recalc_user_id = user.id

        elif data == "ci_feel_note":
            context.user_data["awaiting_note_date"] = yesterday.isoformat()
            reply_text = "What happened? Send me a quick note about yesterday."

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

    if show_feel_step:
        await query.edit_message_text(
            f"{reply_text}\n\nHow did yesterday feel, 1-5?", reply_markup=feel_keyboard()
        )
    elif reply_text:
        await query.edit_message_text(reply_text)
    elif new_keyboard:
        await query.edit_message_reply_markup(reply_markup=new_keyboard)

    if recalc_user_id:
        asyncio.create_task(_run_observation_recalc(recalc_user_id))


async def creatine(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log creatine taken right now, without waiting for a reminder."""
    if update.message is None or update.effective_user is None:
        return
    telegram_id = update.effective_user.id
    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            await update.message.reply_text("Run /start first so I can set you up.")
            return
        target_date = get_user_today(user)
        existing = get_today_log(session, user.id, target_date)
        if existing and existing.taken:
            await update.message.reply_text("Already logged for today ✓")
            return
        mark_taken(session, user.id, target_date)
    await update.message.reply_text("Logged ✓ — creatine taken today.")


async def supplement_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.from_user is None:
        return
    await query.answer()

    data = query.data or ""
    telegram_id = query.from_user.id

    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            return
        target_date = get_user_today(user)

        if data == "supp_take":
            mark_taken(session, user.id, target_date)
            reply_text = "Logged ✓ — creatine taken today."
        elif data == "supp_skip":
            reply_text = "Got it — I'll check again later."
        else:
            return

    await query.edit_message_text(reply_text)


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

        # Skip if question or summary already sent today
        already_sent = session.scalar(
            select(CoachMessage.id).where(
                CoachMessage.user_id == user.id,
                CoachMessage.message_type.in_(["weekly", "weekly_question"]),
                CoachMessage.date == target_date,
            )
        )
        if already_sent:
            await update.message.reply_text("Already sent the weekly check-in today — reply to that message to get your summary.")
            return

        snapshot = build_weekly_snapshot(session, user.id, target_date)
        weekly_memories = memory_retriever.for_weekly(session, user.id)
        payload = build_weekly_payload(
            user, snapshot, now=now, structured_memories=weekly_memories or None
        )

        question = "How did this week feel overall — anything that stood out, good or bad?"
        session.add(CoachMessage(
            user_id=user.id,
            date=target_date,
            message_type="weekly_question",
            summary_payload=payload,
            ai_response=question,
        ))
        session.commit()

    await update.message.reply_text(question)
    log_outgoing(telegram_id, question, "weekly_question")


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


async def goalweight(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set or view target weight in lbs. /goalweight 185"""
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
            if user.goal_weight_lbs is not None:
                await update.message.reply_text(
                    f"Your target weight is {user.goal_weight_lbs} lbs.\n\n"
                    "To update it: /goalweight 185"
                )
            else:
                await update.message.reply_text(
                    "No target weight set yet.\n\nTo set it: /goalweight 185"
                )
            return

        try:
            new_weight = round(float(arg), 1)
        except ValueError:
            await update.message.reply_text("That doesn't look like a number. Try: /goalweight 185")
            return

        if not (50 <= new_weight <= 600):
            await update.message.reply_text(
                "That doesn't look right — enter your goal weight in lbs (e.g. /goalweight 185)."
            )
            return

        user.goal_weight_lbs = new_weight
        session.commit()

    await update.message.reply_text(
        f"Target weight set to {new_weight} lbs. "
        "I'll track your progress in your morning messages and when you ask about weight."
    )


async def memory_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inspect/correct structured memory (Memory 2.0 Phase 2A).

    /memory                 — active memories grouped by type
    /memory recent          — most recently learned memories
    /memory delete <id>     — archive a memory
    /memory confirm <id>    — confirm a memory (raises it to high confidence)
    """
    if update.message is None or update.effective_user is None:
        return
    telegram_id = update.effective_user.id
    args = context.args or []
    sub = args[0].lower() if args else ""

    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            await update.message.reply_text("Run /start first so I can set you up.")
            return

        if sub in ("delete", "confirm"):
            if len(args) < 2 or not args[1].isdigit():
                await update.message.reply_text(f"Usage: /memory {sub} <id>")
                return
            mem_id = int(args[1])
            mem = memory_store.get_memory(session, mem_id)
            if mem is None or mem.user_id != user.id:
                await update.message.reply_text(f"No memory with id {mem_id}.")
                return
            if sub == "delete":
                memory_store.archive(session, mem_id)
                await update.message.reply_text(f"Deleted memory [{mem_id}].")
            else:
                memory_store.confirm(session, mem_id)
                await update.message.reply_text(f"Confirmed memory [{mem_id}] ✓")
            return

        if sub == "recent":
            rows = memory_store.get_active(session, user.id, limit=15)
            text = memory_retriever.render_memory_list(rows, "Recently learned")
        else:
            rows = memory_store.get_active(session, user.id)
            text = memory_retriever.render_memory_list(rows, "What I remember about you")

    # No parse_mode — memory content is AI-extracted and may contain Markdown chars.
    await update.message.reply_text(text)


_RECS_FOLLOW_ACTIONS = {
    "followed": "followed",
    "notfollowed": "not_followed",
    "partial": "partial",
}


async def recs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """View and control logged recommendations (Recommendation Ledger Phase 3C).

    /recs                  recent recommendations (any status)
    /recs recent           same as /recs
    /recs pending          still-pending recommendations / checkpoints
    /recs checked          recently checked outcomes
    /recs cancel <id>      cancel a recommendation
    /recs followed <id>    mark you followed it
    /recs notfollowed <id> mark you didn't follow it
    /recs partial <id>     mark you partly followed it
    """
    if update.message is None or update.effective_user is None:
        return
    telegram_id = update.effective_user.id
    args = context.args or []
    sub = args[0].lower() if args else ""

    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            await update.message.reply_text("Run /start first so I can set you up.")
            return

        # --- control sub-commands: /recs <action> <id> (owner-scoped) ---
        if sub == "cancel" or sub in _RECS_FOLLOW_ACTIONS:
            if len(args) < 2 or not args[1].isdigit():
                await update.message.reply_text(f"Usage: /recs {sub} <id>")
                return
            rec_id = int(args[1])
            rec = recommendation_ledger.get_recommendation(session, rec_id)
            if rec is None or rec.user_id != user.id:
                await update.message.reply_text(f"No recommendation with id {rec_id}.")
                return
            if sub == "cancel":
                recommendation_ledger.cancel(session, rec_id)
                await update.message.reply_text(f"Cancelled recommendation [{rec_id}].")
            else:
                recommendation_ledger.set_followed_status(session, rec_id, _RECS_FOLLOW_ACTIONS[sub])
                await update.message.reply_text(
                    f"Marked recommendation [{rec_id}] as {_RECS_FOLLOW_ACTIONS[sub].replace('_', ' ')}."
                )
            return

        # --- views ---
        if sub == "pending":
            rows = recommendation_ledger.get_pending(session, user.id, limit=20)
            header = "Pending recommendations"
        elif sub == "checked":
            rows = [
                r for r in recommendation_ledger.get_recent(session, user.id, limit=40)
                if r.status in ("checked", "inconclusive")
            ][:20]
            header = "Recently checked"
        else:  # "" or "recent"
            rows = recommendation_ledger.get_recent(session, user.id, limit=20)
            header = "Recent recommendations"
        text = recommendation_ledger.render_recommendation_list(rows, header)

    # No parse_mode — recommendation text is AI-derived and may contain Markdown chars.
    await update.message.reply_text(text)


def _parse_interval_arg(parts: list[str]) -> int | None:
    for p in parts:
        if p.isdigit():
            return int(p)
    return None


async def reta_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Retatrutide shot reminder (user-specified; not medical advice).

    /reta                 log a shot today
    /reta status          show last shot + next due date
    /reta every 6 days    set the interval
    /reta stop            stop reminders
    /reta help            usage
    """
    if update.message is None or update.effective_user is None:
        return
    telegram_id = update.effective_user.id
    args = context.args or []
    sub = args[0].lower() if args else ""

    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            await update.message.reply_text("Run /start first so I can set you up.")
            return
        uid = user.id
        today = get_user_today(user)

        if sub == "help":
            reply = health_reminder.RETA_HELP
        elif sub == "status":
            reply = health_reminder.format_status(health_reminder.get(session, uid))
        elif sub == "stop":
            stopped = health_reminder.stop(session, uid) is not None
            reply = health_reminder.format_stopped(stopped)
        elif sub == "every":
            n = _parse_interval_arg(args[1:])
            if n is None:
                reply = "Usage: /reta every 6 days"
            else:
                try:
                    r = health_reminder.set_interval(session, uid, n)
                    reply = health_reminder.format_set_interval(r)
                except ValueError:
                    reply = "Interval must be between 1 and 365 days (e.g. /reta every 6 days)."
        elif sub == "":
            r = health_reminder.log_completion(session, uid, today)
            reply = health_reminder.format_logged(r, today=today)
        else:
            reply = health_reminder.RETA_HELP

    await update.message.reply_text(reply)
    log_outgoing(telegram_id, reply, "reta", user_id=uid)


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
        focus_memories = memory_retriever.for_focus(session, user.id)
        rec_context = recommendation_ledger.build_context(session, user.id, target_date, limit=3)
        payload = build_weekly_payload(
            user, snapshot, now=now,
            structured_memories=focus_memories or None,
            recommendation_context=rec_context or None,
        )
        user_id = user.id

    try:
        text = await generate_focus_response(payload, user_id=user_id)
    except Exception as exc:
        logger.exception("/focus AI call failed for user %s", telegram_id)
        await send_admin_alert(f"/focus AI call failed for user {telegram_id}: {exc}")
        await update.message.reply_text("Couldn't generate a focus right now — try again in a moment.")
        return

    await update.message.reply_text(text)
    log_outgoing(telegram_id, text, "ai_focus")
    asyncio.create_task(
        recommendation_extractor.run_for_message(
            user_id, text, source_type="focus", source_message_id=None
        )
    )


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

    # Patterns block — split into What Helps / What Hurts
    lines.append("")
    if not observations:
        lines.append("*Patterns:* _Still building — check in daily and patterns will appear here after a few weeks._")
    else:
        helps = [o for o in observations if o.trigger_tag in POSITIVE_TAGS]
        hurts = [o for o in observations if o.trigger_tag not in POSITIVE_TAGS]

        def _obs_lines(obs_list: list) -> None:
            stronger = [o for o in obs_list if o.status == "stronger_signal"]
            promising = [o for o in obs_list if o.status == "promising"]
            watching = [o for o in obs_list if o.status in ("watching", "weak")]
            if stronger:
                lines.append("*Stronger Signals:*")
                for o in stronger:
                    lines.append(f"• {o.pattern_description}\n  {o.supporting_count} of {o.occurrence_count} logged days.")
            if promising:
                lines.append("*Emerging:*")
                for o in promising:
                    lines.append(f"• {o.pattern_description}\n  {o.supporting_count} of {o.occurrence_count} days. Early signal.")
            if watching:
                lines.append("*Watching:*")
                for o in watching:
                    lines.append(f"• {o.pattern_description}\n  {o.supporting_count} of {o.occurrence_count} days so far.")

        if helps:
            lines.append("*What Helps:*")
            _obs_lines(helps)
        if hurts:
            if helps:
                lines.append("")
            lines.append("*What Hurts:*")
            _obs_lines(hurts)

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

    clarify_id_str = context.user_data.pop("clarify_event_id", None)
    if clarify_id_str:
        with SessionLocal() as session:
            ev = session.get(Event, int(clarify_id_str))
            if ev is not None:
                structured = dict(ev.structured or {})
                structured["quantity"] = question
                structured["clarified"] = True
                ev.structured = structured
                ev.confidence = "clean"
                session.commit()
        await update.message.reply_text("Got it ✓")
        log_outgoing(telegram_id, "Got it ✓", "event_log")
        return

    note_date_str = context.user_data.pop("awaiting_note_date", None)
    if note_date_str:
        note_date = date.fromisoformat(note_date_str)
        with SessionLocal() as session:
            user = session.scalar(select(User).where(User.telegram_id == telegram_id))
            if user is None:
                await update.message.reply_text("Run /start first so I can set you up.")
                return
            entry = session.scalar(
                select(JournalEntry).where(
                    JournalEntry.user_id == user.id, JournalEntry.date == note_date
                )
            )
            if entry is None:
                entry = JournalEntry(user_id=user.id, date=note_date, tags=[])
                session.add(entry)
            entry.free_text = question
            session.commit()
            recalc_user_id = user.id
        await update.message.reply_text("Noted ✓")
        log_outgoing(telegram_id, "Noted ✓", "checkin")
        asyncio.create_task(_run_observation_recalc(recalc_user_id))
        return

    # Two-turn weekly reflection: if this is a reply to the weekly question, generate summary
    _weekly_reflection: dict | None = None
    with SessionLocal() as session:
        _wrefl_user = session.scalar(select(User).where(User.telegram_id == telegram_id))
        if _wrefl_user is not None:
            _wrefl_today = get_user_today(_wrefl_user)
            _wq = session.scalar(
                select(CoachMessage).where(
                    CoachMessage.user_id == _wrefl_user.id,
                    CoachMessage.message_type == "weekly_question",
                    CoachMessage.date == _wrefl_today,
                )
            )
            if _wq and not session.scalar(
                select(CoachMessage.id).where(
                    CoachMessage.user_id == _wrefl_user.id,
                    CoachMessage.message_type == "weekly",
                    CoachMessage.date == _wrefl_today,
                )
            ):
                _weekly_reflection = {
                    "user": _wrefl_user,
                    "payload": dict(_wq.summary_payload or {}),
                    "date": _wrefl_today,
                }

    if _weekly_reflection is not None:
        _wr_user = _weekly_reflection["user"]
        _wr_payload = _weekly_reflection["payload"]
        _wr_payload["user_reflection"] = question
        await context.bot.send_chat_action(chat_id=update.message.chat_id, action=ChatAction.TYPING)
        try:
            _wr_msg = await generate_weekly_message(_wr_payload, user_id=_wr_user.id)
        except Exception as exc:
            logger.exception("Weekly reflection AI call failed for user %s", _wr_user.id)
            await send_admin_alert(f"Weekly reflection failed for user {_wr_user.id}: {exc}")
            await update.message.reply_text("I couldn't generate the weekly summary — flagged it.")
            return
        with SessionLocal() as session:
            session.add(CoachMessage(
                user_id=_wr_user.id,
                date=_weekly_reflection["date"],
                message_type="weekly",
                summary_payload=_wr_payload,
                ai_response=_wr_msg,
            ))
            session.commit()
        await update.message.reply_text(_wr_msg)
        log_outgoing(telegram_id, _wr_msg, "ai_weekly")
        return

    # A correction/objection ("that's wrong", "math ain't mathing", "wym") must
    # never be swallowed by the log/reminder detectors — let it continue to Q&A
    # so the coach recomputes.
    is_correction = message_intent.is_correction(question)

    # Current-status memory ("remember I'm taking retatrutide") — store as a fact,
    # never a commitment or event. Skipped for corrections.
    if not is_correction:
        status_phrase = message_intent.detect_status_memory(question)
        if status_phrase:
            with SessionLocal() as session:
                _sm_user = session.scalar(select(User).where(User.telegram_id == telegram_id))
                if _sm_user is not None:
                    _sm_uid = _sm_user.id
                    content = f"Taking {status_phrase}"
                    already = memory_store.find_active_duplicate(session, _sm_uid, "stable_fact", content) is not None
                    memory_store.add_memory(
                        session, _sm_uid, "stable_fact", content,
                        source="user_stated", confidence="high", tags=["status"], dedupe=True,
                    )
            if _sm_user is not None:
                reply = (
                    f"Got it — I already have {status_phrase} in your context."
                    if already else
                    f"Got it — I'll treat {status_phrase} as part of your current context."
                )
                await update.message.reply_text(reply)
                log_outgoing(telegram_id, reply, "memory", user_id=_sm_uid)
                return

    # Stable training constraint/preference ("I can only train mornings before 7am")
    # — captured as memory BEFORE recommendation follow-through so it isn't misread
    # as a follow-through reply.
    if not is_correction:
        constraint = message_intent.detect_constraint_memory(question)
        if constraint:
            _cm_uid = None
            with SessionLocal() as session:
                _cm_user = session.scalar(select(User).where(User.telegram_id == telegram_id))
                if _cm_user is not None:
                    _cm_uid = _cm_user.id
                    already = memory_store.find_active_duplicate(
                        session, _cm_uid, constraint["type"], constraint["content"]
                    ) is not None
                    memory_store.add_memory(
                        session, _cm_uid, constraint["type"], constraint["content"],
                        source="user_stated", confidence="high",
                        tags=["training", "schedule"], dedupe=True,
                    )
            if _cm_uid is not None:
                reply = (
                    f"Got it — I already have that noted: {constraint['content']}."
                    if already else
                    f"Got it — I'll keep that in mind for your training: {constraint['content']}."
                )
                await update.message.reply_text(reply)
                log_outgoing(telegram_id, reply, "memory", user_id=_cm_uid)
                return

    # Retatrutide reminder via natural language: "I took my shot today",
    # "took reta yesterday", "remind me every 6 days for reta".
    with SessionLocal() as session:
        _reta_user = session.scalar(select(User).where(User.telegram_id == telegram_id))
        _reta_uid = _reta_user.id if _reta_user else None
        _reta_today = get_user_today(_reta_user) if _reta_user else None
    if _reta_user is not None and not is_correction:
        reta_intent = health_reminder.detect_reta_message(question, _reta_today)
        if reta_intent is not None:
            with SessionLocal() as session:
                if reta_intent["action"] == "set_interval":
                    try:
                        r = health_reminder.set_interval(session, _reta_uid, reta_intent["interval_days"])
                        reta_reply = health_reminder.format_set_interval(r)
                    except ValueError:
                        reta_reply = "Interval must be between 1 and 365 days."
                else:  # log
                    r = health_reminder.log_completion(session, _reta_uid, reta_intent["date"])
                    reta_reply = health_reminder.format_logged(r, today=_reta_today)
            await update.message.reply_text(reta_reply)
            log_outgoing(telegram_id, reta_reply, "reta", user_id=_reta_uid)
            return

    # Recommendation follow-through: "I skipped training", "stayed under 10", etc.
    # Only considered when there are recent pending recommendations to update.
    ft_pending: list = []
    ft_user_id: int | None = None
    ft_now: dict | None = None
    with SessionLocal() as session:
        _ft_user = session.scalar(select(User).where(User.telegram_id == telegram_id))
        if _ft_user is not None:
            ft_user_id = _ft_user.id
            ft_now = now_block(_ft_user, get_user_now(_ft_user))
            _ft_today = get_user_today(_ft_user)
            ft_pending = [
                r for r in recommendation_ledger.get_pending(session, _ft_user.id, limit=10)
                if (_ft_today - r.local_date).days <= 3
            ]
    if ft_pending and not is_correction and recommendation_followthrough.looks_like_followthrough(question):
        decision = recommendation_followthrough.match_deterministic(question, ft_pending)
        if decision is None:
            decision = await recommendation_followthrough.detect_with_ai(question, ft_pending, ft_now, ft_user_id)
        if decision and decision.get("should_update") and decision.get("recommendation_id"):
            with SessionLocal() as session:
                rec = recommendation_followthrough.apply_decision(session, ft_user_id, decision)
            if rec is not None:
                reply = recommendation_followthrough.confirmation_text(decision.get("followed_status"))
                await update.message.reply_text(reply)
                log_outgoing(telegram_id, reply, "recommendation_followthrough", user_id=ft_user_id)
                return
        elif decision and decision.get("clarifying_question"):
            clarify_q = decision["clarifying_question"]
            await update.message.reply_text(clarify_q)
            log_outgoing(telegram_id, clarify_q, "recommendation_followthrough", user_id=ft_user_id)
            return

    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is not None and not is_correction:
        now = get_user_now(user)
        try:
            extraction = await classify_and_extract(question, now_block(user, now), user_id=user.id)
        except Exception:
            logger.exception("Event extraction failed for user %s", user.id)
            extraction = {"is_log": False, "events": []}
        if extraction.get("is_log") and extraction.get("events"):
            await _handle_logged_events(update, context, user, now, extraction["events"], question)
            return

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
        relevant_memories = memory_retriever.for_qa(session, user.id, question)
        rec_context = recommendation_ledger.build_context(session, user.id, target_date, limit=5)
        payload = build_qa_payload(
            question, qa_ctx, now=now_block(user, now),
            structured_memories=relevant_memories or None,
            recommendation_context=rec_context or None,
        )
        user_id = user.id

        # Last 10 Q&A exchanges as conversation history so the AI remembers the thread
        history_rows = session.scalars(
            select(CoachMessage)
            .where(
                CoachMessage.user_id == user.id,
                CoachMessage.message_type == "q_and_a",
                CoachMessage.ai_response != "",
            )
            .order_by(CoachMessage.id.desc())
            .limit(10)
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
        response_text = await generate_qa_response(payload, history=history, user_id=user_id)
    except Exception as exc:
        logger.exception("Q&A call failed for user %s", user_id)
        await send_admin_alert(f"Q&A call failed for user {user_id}: {exc}")
        await update.message.reply_text("I hit a snag answering that — try again in a moment.")
        return

    # Output guard: block unresolved placeholders ("in about time", "{date}") AND
    # projection dates that contradict the backend's estimated_date. Regenerate
    # once; if still bad, fall back to the deterministic projection text.
    _wp = payload.get("weight_projection")

    def _output_bad(text: str) -> bool:
        return (
            output_guard.has_unresolved_placeholder(text)
            or not output_guard.projection_date_is_consistent(text, _wp)
        )

    if _output_bad(response_text):
        logger.warning("Q&A output guard tripped for user %s — regenerating", user_id)
        repair = (
            "Your previous answer had broken or inconsistent text. Rewrite it cleanly using the "
            "exact numbers in the payload; never output placeholders like 'in about time' or '{date}'."
        )
        if _wp and _wp.get("status") == "projected":
            repair += (
                f" State the timeline exactly as: about {_wp['estimated_weeks']} weeks, "
                f"around {_wp['estimated_date']} — do not change that date."
            )
        try:
            response_text = await generate_qa_response(
                payload, history=history, user_id=user_id, extra_instruction=repair
            )
        except Exception:
            logger.exception("Q&A repair regeneration failed for user %s", user_id)
        if _output_bad(response_text):
            response_text = (
                weight_projection.format_projection(_wp) if _wp
                else "Let me redo that — could you ask again?"
            )

    if msg_id:
        with SessionLocal() as session:
            msg = session.get(CoachMessage, msg_id)
            if msg:
                msg.ai_response = response_text
                session.commit()

    await update.message.reply_text(response_text)
    log_outgoing(telegram_id, response_text, "q_and_a")

    # Background: extract persistent facts from this exchange and merge into coach_notes.
    # Legacy coach_notes extraction (about_you) runs alongside the new structured
    # memory extraction (user_memories) during Phase 2A — neither blocks the reply.
    asyncio.create_task(_update_coach_notes(user_id, question, response_text))
    asyncio.create_task(_run_memory_extraction(user_id, question, response_text))
    # Background: extract any checkable advice from the Q&A answer into the ledger.
    asyncio.create_task(
        recommendation_extractor.run_for_message(
            user_id, response_text, source_type="qa", source_message_id=msg_id
        )
    )


async def _run_memory_extraction(user_id: int, user_message: str, ai_response: str) -> None:
    """Background: extract typed structured memories into user_memories (Phase 2A).

    Runs in parallel with _update_coach_notes. memory_extractor.run_for_exchange
    is already crash-proof; this wrapper is belt-and-suspenders so a failure here
    never affects the reply the user already received."""
    try:
        await memory_extractor.run_for_exchange(user_id, user_message, ai_response)
    except Exception:
        logger.exception("Structured memory extraction failed for user %s", user_id)


async def _update_coach_notes(user_id: int, user_message: str, ai_response: str) -> None:
    """Extract lasting personal facts from a Q&A exchange and save to user.coach_notes."""
    try:
        with SessionLocal() as session:
            user = session.get(User, user_id)
            if user is None:
                return
            existing = user.coach_notes if isinstance(user.coach_notes, dict) else {}

        new_facts = await extract_user_facts(user_message, ai_response, existing, user_id=user_id)
        if not new_facts:
            return

        with SessionLocal() as session:
            user = session.get(User, user_id)
            if user is None:
                return
            merged = dict(user.coach_notes) if isinstance(user.coach_notes, dict) else {}
            for key, val in new_facts.items():
                if isinstance(val, list):
                    existing_list = merged.get(key, [])
                    for item in val:
                        if item not in existing_list:
                            existing_list.append(item)
                    merged[key] = existing_list
                else:
                    merged[key] = val
            user.coach_notes = merged
            session.commit()
    except Exception:
        pass  # never let background fact extraction crash anything


HELP_TEXT = """\
*Body Manual AI — what I can do:*

*Daily*
/today — morning coach message (recovery, sleep, HRV vs your baselines)
/checkin — log what happened yesterday (tap tags or add a note)
/focus — one action item for this week

*Log anything, anytime*
Just type it — "had pizza at 9pm", "3 drinks tonight", "stressful day", "going to bed before 11 this week". I'll log it and connect it to how your body responds.

*Review*
/weekly — this week vs your 30-day baseline (asks how the week felt first)
/manual — your baselines, patterns, and what helps vs hurts
/memory — what I've learned about you (/memory delete <id> to fix it)
/recs — recommendations I've made and how they turned out
/history — last 7 daily messages
/chatlog — full conversation history

*Track*
/experiment — start a self-test (e.g. "does creatine affect my recovery?")
/creatine — log creatine and get reminders
/reta — log your retatrutide shot; reminds you on the due date (/reta help)

*Settings*
/goal — general health / performance / weight loss
/timezone — view or update your timezone
/connect_whoop — connect or reconnect WHOOP
/connect_withings — connect Withings scale
/backfill — re-pull up to 365 days of history
/delete — permanently erase all your data"""


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


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


_EVENT_TYPE_LABEL = {
    "meal": "a meal",
    "alcohol": "a drink",
    "caffeine": "caffeine",
    "stress": "stress",
    "exercise": "a workout",
    "sleep_problem": "a sleep issue",
    "note": "that",
}


def _confirm_event_text(event_types: list[str]) -> str:
    labels = [_EVENT_TYPE_LABEL.get(t, t) for t in event_types]
    return f"Logged ✓ — {', '.join(labels)}."


async def _handle_logged_events(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
    now: datetime,
    events: list[dict],
    raw_text: str,
) -> None:
    """Persist extracted events, roll them into the existing tag vocabulary, and
    either confirm the log or ask the one allowed clarifying question."""
    telegram_id = user.telegram_id
    clarifying_question: str | None = None
    clarify_event_id: int | None = None
    saved_types: list[str] = []
    recalc_user_id: int | None = None

    with SessionLocal() as session:
        for e in events:
            event_type = e.get("event_type") or "note"
            if event_type not in EVENT_TYPES:
                event_type = "note"
            time_phrase = e.get("time_phrase") or ""
            occurred_at = resolve_local_time(time_phrase, now) or now
            confidence = "needs_confirmation" if e.get("confidence") == "needs_confirmation" else "clean"

            row = Event(
                user_id=user.id,
                occurred_at=occurred_at,
                local_date=occurred_at.date(),
                event_type=event_type,
                raw_text=raw_text,
                structured={"quantity": e.get("quantity"), "time_phrase": time_phrase},
                confidence=confidence,
                source="chat",
            )
            session.add(row)
            session.flush()
            saved_types.append(event_type)
            apply_event_to_tags(session, user.id, event_type, occurred_at)

            if confidence == "needs_confirmation" and clarifying_question is None:
                clarifying_question = e.get("clarifying_question") or "Can you say a bit more about that?"
                clarify_event_id = row.id

        session.commit()
        recalc_user_id = user.id

    if clarifying_question:
        context.user_data["clarify_event_id"] = str(clarify_event_id)
        await update.message.reply_text(clarifying_question)
        log_outgoing(telegram_id, clarifying_question, "event_log", user_id=user.id)
    else:
        reply = _confirm_event_text(saved_types)
        await update.message.reply_text(reply)
        log_outgoing(telegram_id, reply, "event_log", user_id=user.id)

    asyncio.create_task(_run_observation_recalc(recalc_user_id))


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
