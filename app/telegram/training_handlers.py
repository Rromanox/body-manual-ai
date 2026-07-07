"""Telegram command + callback handlers for the training-plan module.

Thin layer: every mutation goes through the shared services (training_plan,
training_rules, training_gate, training_substitution) so these handlers and the
natural-language flow in handlers.plain_text can never diverge.
"""
from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import select
from telegram import Update
from telegram.ext import ContextTypes

from app.db import SessionLocal
from app.models.user import User
from app.services import training_format as fmt
from app.services import training_gate as gate
from app.services import training_plan as tp
from app.services import training_rules as rules
from app.services import training_substitution as subs
from app.services.chat_logger import log_outgoing
from app.services.timekit import get_user_today
from app.telegram import keyboards

logger = logging.getLogger(__name__)


async def _reply(update: Update, text: str, uid: int | None, *, reply_markup=None) -> None:
    await update.message.reply_text(text, reply_markup=reply_markup)
    if update.effective_user:
        log_outgoing(update.effective_user.id, text, "training", user_id=uid)


def _user(session, telegram_id: int) -> User | None:
    return session.scalar(select(User).where(User.telegram_id == telegram_id))


# --- read commands ----------------------------------------------------------

async def week_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    with SessionLocal() as session:
        user = _user(session, update.effective_user.id)
        if user is None:
            await update.message.reply_text("Run /start first so I can set you up.")
            return
        today = get_user_today(user)
        week = None
        if context.args and context.args[0].isdigit():
            week = max(1, min(tp.PLAN_WEEKS, int(context.args[0])))
        week = week or tp.week_of(today) or 1
        rows = tp.get_week(session, user.id, week)
        text = fmt.format_week(rows, week)
        uid = user.id
    await _reply(update, text, uid)


async def plan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    with SessionLocal() as session:
        user = _user(session, update.effective_user.id)
        if user is None:
            await update.message.reply_text("Run /start first so I can set you up.")
            return
        text = fmt.format_plan(tp.plan_overview(session, user.id, get_user_today(user)))
        uid = user.id
    await _reply(update, text, uid)


# --- mutation commands ------------------------------------------------------

async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    with SessionLocal() as session:
        user = _user(session, update.effective_user.id)
        if user is None:
            await update.message.reply_text("Run /start first so I can set you up.")
            return
        today = get_user_today(user)
        args = list(context.args or [])
        d = today
        if args and (parsed := fmt.parse_plan_date(args[0], today)):
            d = parsed
            args = args[1:]
        notes = " ".join(args) or None
        row = tp.mark_completed(session, user.id, d, notes=notes, source="command")
        if row is None:
            text = f"Nothing to complete on {d:%b} {d.day} (rest day or no session)."
        else:
            text = f"✅ Logged {row.title} for {fmt._day_label(d)}."
        uid = user.id
    await _reply(update, text, uid)


def _skip_reply(out: dict) -> tuple[str, object | None]:
    outcome = out["outcome"]
    if outcome == "noop":
        return "Nothing scheduled to skip there.", None
    if outcome == "moved":
        to = out["to"]
        return f"🔁 Moved your Saturday ride to Sunday {to:%b} {to.day} — Sunday's easy Z2 is canceled.", None
    if outcome == "skipped" and out["rule"] == "high_saturday_sunday_taken":
        return "⏭ Skipped — couldn't shift it to Sunday (recovery week or the day's taken).", None
    if outcome == "skipped":
        return "⏭ Skipped. No reschedule — consistency beats cramming a make-up in.", None
    if outcome == "needs_choice":
        d = out["session_date"]
        return (
            "That's a ⭐ critical ride — I won't drop it silently. Move it to:",
            keyboards.critical_choice_keyboard(d.isoformat()),
        )
    return "Done.", None


async def skip_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    with SessionLocal() as session:
        user = _user(session, update.effective_user.id)
        if user is None:
            await update.message.reply_text("Run /start first so I can set you up.")
            return
        today = get_user_today(user)
        args = list(context.args or [])
        d = today
        if args and (parsed := fmt.parse_plan_date(args[0], today)):
            d = parsed
            args = args[1:]
        reason = " ".join(args) or None
        out = rules.skip_session(session, user.id, d, reason=reason, source="command")
        text, markup = _skip_reply(out)
        uid = user.id
    await _reply(update, text, uid, reply_markup=markup)


async def move_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    with SessionLocal() as session:
        user = _user(session, update.effective_user.id)
        if user is None:
            await update.message.reply_text("Run /start first so I can set you up.")
            return
        today = get_user_today(user)
        raw = " ".join(context.args or [])
        parts = raw.split(" to ")
        if len(parts) != 2:
            await update.message.reply_text("Usage: /move <date> to <date>  (e.g. /move Saturday to Sunday)")
            return
        from_d = fmt.parse_plan_date(parts[0], today)
        to_d = fmt.parse_plan_date(parts[1], today)
        if from_d is None or to_d is None:
            await update.message.reply_text("I couldn't read those dates. Try /move 2026-08-15 to 2026-08-16.")
            return
        out = rules.move_session(session, user.id, from_d, to_d, source="command")
        uid = user.id
        text, markup = _move_reply(out, from_d, to_d)
    await _reply(update, text, uid, reply_markup=markup)


def _move_reply(out: dict, from_d: date, to_d: date):
    if out["outcome"] == "rejected" and out.get("rule") == "protected_week":
        return f"Can't move into Week {out['week']} — recovery/taper weeks never gain sessions.", None
    if out["outcome"] == "rejected":
        return "That date is outside the training plan.", None
    if out["outcome"] == "noop":
        return "Nothing to move on that date.", None
    if out["outcome"] == "needs_confirm_swap":
        return (
            f"{to_d:%b} {to_d.day} already has \"{out['target_title']}\". Swap them?",
            keyboards.move_swap_keyboard(from_d.isoformat(), to_d.isoformat()),
        )
    return f"🔁 Moved to {to_d:%b} {to_d.day}.", None


async def edit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    with SessionLocal() as session:
        user = _user(session, update.effective_user.id)
        if user is None:
            await update.message.reply_text("Run /start first so I can set you up.")
            return
        today = get_user_today(user)
        args = list(context.args or [])
        if not args:
            await update.message.reply_text("Usage: /edit <date>  (e.g. /edit Saturday)")
            return
        d = fmt.parse_plan_date(args[0], today)
        if d is None:
            await update.message.reply_text("I couldn't read that date. Try /edit 2026-08-15.")
            return
        row = tp.get_session(session, user.id, d)
        if row is None:
            await update.message.reply_text(f"No session on {d:%b} {d.day} to edit.")
            return
        uid = user.id
        # Inline args form: /edit <date> duration 75 | type z2 | title ...
        if len(args) >= 3:
            field, value = args[1].lower(), " ".join(args[2:])
            text = _apply_edit(session, uid, d, field, value)
            await _reply(update, text, uid)
            return
    await _reply(
        update, f"What do you want to change for {fmt._day_label(d)} — \"{row.title}\"?",
        uid, reply_markup=keyboards.edit_keyboard(d.isoformat()),
    )


def _apply_edit(session, uid: int, d: date, field: str, value: str) -> str:
    try:
        if field in ("duration", "duration_min", "time"):
            row = tp.edit_session(session, uid, d, duration_min=int(value), source="command")
            return f"✏️ {fmt._day_label(d)}: duration set to {value} min."
        if field in ("type", "intensity", "session_type"):
            row = tp.edit_session(session, uid, d, session_type=value.lower(), source="command")
            return f"✏️ {fmt._day_label(d)}: type set to {value.lower()}."
        if field == "title":
            tp.edit_session(session, uid, d, title=value, source="command")
            return f"✏️ {fmt._day_label(d)}: title updated."
    except ValueError:
        return "That value didn't validate. Duration must be a number; type must be one of intervals/z2/tempo/gym_a/gym_b/long_ride/rest."
    return "Nothing changed — say duration/type/title and a value."


async def cant_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    with SessionLocal() as session:
        user = _user(session, update.effective_user.id)
        if user is None:
            await update.message.reply_text("Run /start first so I can set you up.")
            return
        today = get_user_today(user)
        row = tp.get_session(session, user.id, today)
        uid = user.id
        if row is None or row.session_type == "rest":
            await _reply(update, "Today's a rest day — nothing to change.", uid)
            return
    await _reply(
        update, "What's getting in the way today?", uid,
        reply_markup=keyboards.cant_keyboard(today.isoformat()),
    )


# --- callbacks --------------------------------------------------------------

_SUBST_MAP = {"less_time", "no_bike", "cant_leave", "feeling_beat", "skip"}


def _subst_reply(session, uid: int, out: dict, d: date):
    """(text, markup) for a substitution outcome."""
    outcome = out["outcome"]
    if outcome == "needs_minutes":
        return "How much time do you have?", keyboards.less_time_keyboard(d.isoformat())
    if outcome == "substituted":
        tail = " (maintenance stimulus only)" if out.get("maintenance_only") else ""
        return f"✳️ {out['text']}{tail}", None
    if outcome == "refused_critical":
        return (
            "That's a ⭐ critical ride — I won't substitute it. Move it instead:",
            keyboards.critical_choice_keyboard(d.isoformat()),
        )
    if outcome == "not_substitutable":
        return (f"A long ride can't be replaced off the bike. Move it with /move {d.isoformat()} to <date>.", None)
    if outcome == "route_to_skip":
        return _skip_reply(rules.skip_session(session, uid, d, source="natural_language"))
    if outcome == "route_to_gate":
        g = gate.evaluate_gate(session, uid, d)
        markup = None
        if g.adjustment_offered:
            gate.record_recommendation(session, uid, d, g, source="natural_language")
            markup = keyboards.gate_keyboard(d.isoformat())
        return "\n".join([g.recovery_line, *g.notes]), markup
    return out.get("message", "Nothing changed."), None


async def training_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.from_user is None:
        return
    await query.answer()
    parts = (query.data or "").split(":")
    if len(parts) < 2:
        return
    kind, arg = parts[0], parts[1]
    telegram_id = query.from_user.id

    text = "Done."
    markup = None
    with SessionLocal() as session:
        user = _user(session, telegram_id)
        if user is None:
            return
        uid = user.id
        try:
            d = date.fromisoformat(parts[2]) if len(parts) > 2 else get_user_today(user)
        except ValueError:
            d = get_user_today(user)

        if kind == "tr_gate":
            if arg == "accept":
                gate.accept_adjustment(session, uid, d, source="command")
                text = "✅ Adjustment accepted — logged as an adjusted session."
            else:
                gate.override_adjustment(session, uid, d, source="command")
                text = "👍 Riding it as written."
        elif kind == "tr_crit":
            choice = "sunday" if arg == "sunday" else "next_saturday"
            out = rules.apply_critical_choice(session, uid, d, choice, source="command")
            to = out.get("to")
            text = f"🔁 Moved the critical ride to {to:%b} {to.day}." if to else "Couldn't move it."
        elif kind == "tr_cant":
            out = subs.substitute(session, uid, d, arg, source="command")
            text, markup = _subst_reply(session, uid, out, d)
        elif kind == "tr_time":
            out = subs.substitute(session, uid, d, "less_time", minutes=int(arg), source="command")
            text, markup = _subst_reply(session, uid, out, d)
        elif kind == "tr_edit":
            field = "duration" if arg == "duration" else "type"
            context.user_data["tr_edit"] = {"date": d.isoformat(), "field": field}
            if field == "duration":
                text = f"Send the new duration in minutes for {fmt._day_label(d)}."
            else:
                text = "Send the new type: intervals, z2, tempo, gym_a, gym_b, long_ride, or rest."
        elif kind == "tr_move":
            if arg == "swap":
                to_d = date.fromisoformat(parts[3])
                rules.move_session(session, uid, d, to_d, source="command", confirm_swap=True)
                text = f"🔁 Swapped {fmt._day_label(d)} ↔ {fmt._day_label(to_d)}."
            else:
                text = "Okay, left it as is."

    await query.edit_message_text(text, reply_markup=markup)
    log_outgoing(telegram_id, text, "training", user_id=uid)

