from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

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
from app.services.ai_client import generate_daily_message, generate_qa_response, generate_weekly_message
from app.services.alerts import send_admin_alert
from app.services.baseline_engine import (
    build_daily_snapshot,
    build_qa_context,
    build_weekly_snapshot,
    safety_message,
)
from app.services.coach_payload_builder import build_daily_payload, build_qa_payload, build_weekly_payload
from app.services.observation_engine import recalculate_observations
from app.services.whoop_client import WhoopAuthError, build_authorize_url, make_oauth_state
from app.services.withings_client import build_authorize_url as withings_authorize_url
from app.telegram.keyboards import checkin_keyboard, confirm_delete_keyboard

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

        target_date = _today_local(user)
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
        payload = build_daily_payload(user, snapshot, yesterday_tags=yesterday_tags, today_metric_row=today_row)

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

        target_date = _today_local(user)
        snapshot = build_weekly_snapshot(session, user.id, target_date)
        payload = build_weekly_payload(user, snapshot)

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


async def manual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    telegram_id = update.effective_user.id

    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            await update.message.reply_text("Run /start first so I can set you up.")
            return

        observations = session.scalars(
            select(Observation)
            .where(Observation.user_id == user.id, Observation.status != "archived")
            .order_by(Observation.occurrence_count.desc())
        ).all()
        name = user.first_name or "Your"

    lines = [f"*{name}'s Body Manual*\n"]

    if not observations:
        lines.append(
            "_Still building your manual — I need check-in data over several weeks to start "
            "spotting patterns. Use /checkin each morning to help me learn._"
        )
    else:
        stronger = [o for o in observations if o.status == "stronger_signal"]
        promising = [o for o in observations if o.status == "promising"]
        watching = [o for o in observations if o.status in ("watching", "weak")]

        if stronger:
            lines.append("*Stronger Signals:*")
            for o in stronger:
                lines.append(
                    f"• {o.pattern_description}\n  Evidence: {o.supporting_count} of {o.occurrence_count} logged days."
                )
        if promising:
            lines.append("\n*Emerging Patterns:*")
            for o in promising:
                lines.append(
                    f"• {o.pattern_description}\n  Evidence: {o.supporting_count} of {o.occurrence_count} logged days. Early signal."
                )
        if watching:
            lines.append("\n*Watching:*")
            for o in watching:
                lines.append(
                    f"• {o.pattern_description}\n  {o.supporting_count} of {o.occurrence_count} days so far."
                )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


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

    await update.message.reply_text("Pulling your last 365 days from WHOOP — this may take a minute…")
    try:
        with SessionLocal() as session:
            user = session.get(User, user_id)
            written = await pull_and_store(session, user, days=365)
        await update.message.reply_text(f"Done — {written} days of data loaded.")
    except WhoopAuthError as exc:
        await send_admin_alert(f"WHOOP auth failed for user {user_id} during /backfill: {exc}")
        await update.message.reply_text("Your WHOOP connection stopped working — reconnect with /connect_whoop.")
    except Exception as exc:
        logger.exception("/backfill failed for user %s", user_id)
        await send_admin_alert(f"/backfill failed for user {user_id}: {exc}")
        await update.message.reply_text("Something went wrong pulling the data — I've flagged it.")


async def plain_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Any non-command message is a question to the coach."""
    if update.message is None or update.effective_user is None:
        return
    question = (update.message.text or "").strip()
    if not question:
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

        target_date = _today_local(user)
        qa_ctx = build_qa_context(session, user.id, target_date)
        payload = build_qa_payload(question, qa_ctx)
        user_id = user.id

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
        response_text = await generate_qa_response(payload)
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


async def _run_observation_recalc(user_id: int) -> None:
    try:
        with SessionLocal() as session:
            recalculate_observations(session, user_id)
    except Exception:
        logger.exception("Observation recalc failed for user %s", user_id)


def _today_local(user: User) -> date:
    return datetime.now(ZoneInfo(user.timezone)).date()


def _has_active_whoop(session, user_id: int) -> bool:
    conn = session.scalar(
        select(OAuthConnection).where(
            OAuthConnection.user_id == user_id,
            OAuthConnection.provider == "whoop",
            OAuthConnection.status == "active",
        )
    )
    return conn is not None
