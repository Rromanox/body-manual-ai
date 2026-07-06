"""Recurring health reminders — retatrutide shot (`/reta`).

A user-specified reminder ONLY: it tracks an interval and a next-due date and
nudges on the due date. It never gives dosage or medical advice. The next due
date is always recomputed from the actual logged completion date, so a late log
shifts the schedule forward.

Data ops are deterministic; natural-language detection is deterministic regex
(no AI needed for these clear patterns). Shared by the /reta command and the
free-text flow so both behave identically.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.health_reminder import HealthReminder

logger = logging.getLogger(__name__)

RETA_TYPE = "retatrutide"
RETA_NAME = "Retatrutide"
DEFAULT_INTERVAL_DAYS = 6
_MAX_INTERVAL_DAYS = 365

RETA_HELP = (
    "/reta — log retatrutide shot today\n"
    "/reta status — show last shot and next due date\n"
    "/reta every 6 days — set the interval\n"
    "/reta stop — stop reminders"
)


# --- data ops ---------------------------------------------------------------

def get(session: Session, user_id: int, reminder_type: str = RETA_TYPE) -> HealthReminder | None:
    return session.scalar(
        select(HealthReminder).where(
            HealthReminder.user_id == user_id,
            HealthReminder.reminder_type == reminder_type,
        )
    )


def log_completion(
    session: Session,
    user_id: int,
    completed_date: date,
    *,
    reminder_type: str = RETA_TYPE,
    name: str = RETA_NAME,
    default_interval: int = DEFAULT_INTERVAL_DAYS,
    commit: bool = True,
) -> HealthReminder:
    """Record a shot for ``completed_date`` and recompute the next due date.

    Creates the reminder (default interval) if it doesn't exist. Next due is
    always ``completed_date + interval`` — so a late log moves the schedule."""
    reminder = get(session, user_id, reminder_type)
    if reminder is None:
        reminder = HealthReminder(
            user_id=user_id, reminder_type=reminder_type, name=name,
            interval_days=default_interval,
        )
        session.add(reminder)
    reminder.last_completed_date = completed_date
    reminder.next_due_date = completed_date + timedelta(days=reminder.interval_days)
    reminder.is_active = True
    reminder.last_reminded_date = None  # fresh cycle; allow a reminder on the new due date
    if commit:
        session.commit()
    logger.info(
        "health_reminder log user=%s type=%s completed=%s next_due=%s",
        user_id, reminder_type, completed_date, reminder.next_due_date,
    )
    return reminder


def set_interval(
    session: Session,
    user_id: int,
    interval_days: int,
    *,
    reminder_type: str = RETA_TYPE,
    name: str = RETA_NAME,
    commit: bool = True,
) -> HealthReminder:
    """Create or update the interval. Recomputes next due from the last
    completion when one exists; otherwise leaves next due unset until first log."""
    if not isinstance(interval_days, int) or not (1 <= interval_days <= _MAX_INTERVAL_DAYS):
        raise ValueError(f"interval_days must be 1..{_MAX_INTERVAL_DAYS}")
    reminder = get(session, user_id, reminder_type)
    if reminder is None:
        reminder = HealthReminder(
            user_id=user_id, reminder_type=reminder_type, name=name,
            interval_days=interval_days,
        )
        session.add(reminder)
    else:
        reminder.interval_days = interval_days
        if reminder.last_completed_date is not None:
            reminder.next_due_date = reminder.last_completed_date + timedelta(days=interval_days)
    reminder.is_active = True
    if commit:
        session.commit()
    return reminder


def stop(session: Session, user_id: int, reminder_type: str = RETA_TYPE, *, commit: bool = True) -> HealthReminder | None:
    reminder = get(session, user_id, reminder_type)
    if reminder is None:
        return None
    reminder.is_active = False
    if commit:
        session.commit()
    return reminder


def due_reminders(session: Session, user_id: int, today: date) -> list[HealthReminder]:
    """Active reminders that should fire today: due, not already reminded today,
    and not already completed today."""
    rows = session.scalars(
        select(HealthReminder).where(
            HealthReminder.user_id == user_id,
            HealthReminder.is_active.is_(True),
            HealthReminder.next_due_date.is_not(None),
            HealthReminder.next_due_date <= today,
        )
    ).all()
    return [
        r for r in rows
        if r.last_reminded_date != today and r.last_completed_date != today
    ]


def mark_reminded(
    session: Session, reminder_id: int, today: date, *, now: datetime | None = None, commit: bool = True
) -> None:
    reminder = session.get(HealthReminder, reminder_id)
    if reminder is None:
        return
    reminder.last_reminded_date = today
    reminder.last_reminded_at = now or datetime.now(timezone.utc)
    if commit:
        session.commit()


# How long after a reminder a bare "yes"/"done" still counts as confirming it.
_CONFIRM_WINDOW_HOURS = 6


def awaiting_confirmation(
    session: Session,
    user_id: int,
    now: datetime,
    *,
    within_hours: int = _CONFIRM_WINDOW_HOURS,
    reminder_type: str = RETA_TYPE,
) -> HealthReminder | None:
    """The active reminder awaiting confirmation: reminded within ``within_hours``
    and not yet completed today. Lets a bare "yes"/"done" reply log the shot
    (Bug #1) without hijacking unrelated "yes" messages."""
    reminder = get(session, user_id, reminder_type)
    if reminder is None or not reminder.is_active or reminder.last_reminded_at is None:
        return None
    if reminder.last_completed_date == now.date():
        return None  # already logged today
    last_at = reminder.last_reminded_at
    if last_at.tzinfo is None:
        last_at = last_at.replace(tzinfo=timezone.utc)
    if now - last_at <= timedelta(hours=within_hours):
        return reminder
    return None


# --- natural-language detection (deterministic) -----------------------------

# Reta or "my/the shot" — keeps "took a shot of espresso" from matching.
_RETA_SIGNAL_RE = re.compile(r"\b(reta|retatrutide)\b|\b(my|the)\s+shot\b", re.IGNORECASE)
_INTERVAL_RE = re.compile(r"every\s+(\d+)\s*days?", re.IGNORECASE)
_REMIND_RE = re.compile(r"\bremind\b", re.IGNORECASE)
# Past-tense completion only (not "I take..."/"I'll take...").
_TAKEN_RE = re.compile(r"\b(took|taken|did|had|injected|done)\b", re.IGNORECASE)

# A short affirmative reply — counts as confirmation only when a reminder was just
# sent (see awaiting_confirmation). Kept tight so "yesterday..." / "yes but..." miss.
_BARE_CONFIRM_RE = re.compile(
    r"^(?:(?:yes|yep|yeah|yup|done|taken|confirmed|took it|did it|already (?:took|did)|all done)\b|👍|✅)",
    re.IGNORECASE,
)


def is_bare_confirmation(message: str) -> bool:
    msg = (message or "").strip()
    if not msg or len(msg) > 25:
        return False
    return bool(_BARE_CONFIRM_RE.match(msg))
_TODAY_RE = re.compile(r"\b(today|this morning|this evening|tonight|just now)\b", re.IGNORECASE)
_YESTERDAY_RE = re.compile(r"\b(yesterday|last night)\b", re.IGNORECASE)


def detect_reta_message(message: str, today: date) -> dict | None:
    """Detect a retatrutide log or interval-set intent in a free-text message.

    Returns {"action": "log", "date": <date>} or
    {"action": "set_interval", "interval_days": <int>}, or None when the message
    isn't clearly about the retatrutide reminder (so Q&A/logging are untouched)."""
    msg = (message or "").strip()
    if not msg or msg.endswith("?"):
        return None
    if not _RETA_SIGNAL_RE.search(msg):
        return None

    interval = _INTERVAL_RE.search(msg)
    if _REMIND_RE.search(msg) and interval:
        return {"action": "set_interval", "interval_days": int(interval.group(1))}

    if _TAKEN_RE.search(msg):
        # "today" wins when both appear ("forgot yesterday but took it today").
        if _TODAY_RE.search(msg):
            d = today
        elif _YESTERDAY_RE.search(msg):
            d = today - timedelta(days=1)
        else:
            d = today
        return {"action": "log", "date": d}

    return None


# --- reply formatting -------------------------------------------------------

def _fmt(d: date | None) -> str:
    return f"{d.strftime('%b')} {d.day}" if d else "—"


def format_logged(reminder: HealthReminder, today: date | None = None) -> str:
    completed = reminder.last_completed_date
    if today is not None and completed == today:
        when = "today"
    elif today is not None and completed == today - timedelta(days=1):
        when = "yesterday"
    else:
        when = _fmt(completed)
    return f"Logged {reminder.name.lower()} for {when}. Next due: {_fmt(reminder.next_due_date)}."


def format_status(reminder: HealthReminder | None) -> str:
    if reminder is None or not reminder.is_active:
        return (
            "Retatrutide reminder is not set yet. Use /reta every 6 days or /reta to start."
            if reminder is None else
            "Retatrutide reminders are stopped. Use /reta or /reta every 6 days to restart."
        )
    last = _fmt(reminder.last_completed_date) if reminder.last_completed_date else "never"
    due = _fmt(reminder.next_due_date) if reminder.next_due_date else "after your first log"
    return (
        f"{reminder.name}: last logged {last}, next due {due}. "
        f"Interval: every {reminder.interval_days} days."
    )


def format_set_interval(reminder: HealthReminder) -> str:
    base = f"{reminder.name} reminder set to every {reminder.interval_days} days."
    if reminder.last_completed_date is None:
        return base + " Log your first shot with /reta."
    return base + f" Next due: {_fmt(reminder.next_due_date)}."


def format_stopped(stopped: bool) -> str:
    return "Retatrutide reminders stopped." if stopped else "No retatrutide reminder to stop."
