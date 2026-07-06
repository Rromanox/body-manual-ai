"""Training-plan data layer — the ONE shared service both the Telegram commands
and the natural-language flow call, so the two paths can never diverge.

The backend owns all plan state and mutations here; the AI only ever narrates the
results (same principle as the rest of the app). Every mutation writes a row to
``training_log`` via :func:`log_action`, including which rule fired.

Choices are validated against the module-level constant sets rather than a DB
enum, matching the codebase convention (health_reminders.reminder_type etc.).
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.training_log import TrainingLog
from app.models.training_session import TrainingSession

logger = logging.getLogger(__name__)

# --- vocabulary -------------------------------------------------------------

PHASES = frozenset({"base", "build", "specificity", "taper"})
SESSION_TYPES = frozenset(
    {"intervals", "z2", "tempo", "gym_a", "gym_b", "long_ride", "rest"}
)
PRIORITIES = frozenset({"normal", "high", "critical"})
STATUSES = frozenset({"pending", "completed", "skipped", "modified", "moved"})
ACTIONS = frozenset(
    {
        "seeded", "completed", "skipped", "moved", "edited", "substituted",
        "gate_recommendation", "gate_accepted", "gate_overridden",
    }
)
SOURCES = frozenset({"command", "natural_language", "system"})

# "done" for progress accounting: a session that was actually trained, whether
# as written (completed) or in an adjusted/substituted form (modified).
DONE_STATUSES = frozenset({"completed", "modified"})

# --- plan calendar ----------------------------------------------------------

PLAN_START = date(2026, 7, 13)   # Monday, week 1
PLAN_END = date(2026, 10, 4)     # Sunday, end of week 12 (trip start)
PLAN_WEEKS = 12

# The three ⭐ long rides that are never dropped or substituted silently.
CRITICAL_RIDE_DATES = frozenset({date(2026, 9, 5), date(2026, 9, 12), date(2026, 9, 19)})

# Weeks that never gain sessions (recovery weeks + the taper).
PROTECTED_WEEKS = frozenset({4, 7, 12})


def week_of(d: date) -> int | None:
    """Plan week (1..12) for a date, or None if outside the plan window."""
    if d < PLAN_START or d > PLAN_END:
        return None
    return (d - PLAN_START).days // 7 + 1


def phase_for_week(week: int) -> str:
    if week <= 4:
        return "base"
    if week <= 8:
        return "build"
    if week <= 11:
        return "specificity"
    return "taper"


def week_date_range(week: int) -> tuple[date, date]:
    """(Monday, Sunday) local dates bounding a plan week."""
    start = PLAN_START + timedelta(days=(week - 1) * 7)
    return start, start + timedelta(days=6)


# --- validation -------------------------------------------------------------

def _require(value: str, allowed: frozenset[str], label: str) -> str:
    if value not in allowed:
        raise ValueError(f"invalid {label}: {value!r} (allowed: {sorted(allowed)})")
    return value


# --- reads ------------------------------------------------------------------

def get_session(session: Session, user_id: int, d: date) -> TrainingSession | None:
    return session.scalar(
        select(TrainingSession).where(
            TrainingSession.user_id == user_id, TrainingSession.date == d
        )
    )


def get_week(session: Session, user_id: int, week: int) -> list[TrainingSession]:
    """All sessions (rest days included) for a plan week, ordered by date."""
    start, end = week_date_range(week)
    return list(
        session.scalars(
            select(TrainingSession)
            .where(
                TrainingSession.user_id == user_id,
                TrainingSession.date >= start,
                TrainingSession.date <= end,
            )
            .order_by(TrainingSession.date)
        ).all()
    )


def critical_rides(session: Session, user_id: int) -> list[TrainingSession]:
    return list(
        session.scalars(
            select(TrainingSession)
            .where(
                TrainingSession.user_id == user_id,
                TrainingSession.priority == "critical",
            )
            .order_by(TrainingSession.date)
        ).all()
    )


def plan_overview(session: Session, user_id: int, today: date) -> dict[str, Any]:
    """Phase overview for /plan: current week/phase, completion %, critical rides
    done vs remaining."""
    rows = list(
        session.scalars(
            select(TrainingSession).where(TrainingSession.user_id == user_id)
        ).all()
    )
    # Exclude relocated (moved) rows — they're represented by their target day, so
    # counting both would double the denominator.
    non_rest = [r for r in rows if r.session_type != "rest" and r.status != "moved"]
    done = [r for r in non_rest if r.status in DONE_STATUSES]
    crit = [r for r in rows if r.priority == "critical"]
    crit_done = [r for r in crit if r.status in DONE_STATUSES]

    current_week = week_of(today)
    completion_pct = round(len(done) / len(non_rest) * 100) if non_rest else 0
    return {
        "current_week": current_week,
        "current_phase": phase_for_week(current_week) if current_week else None,
        "total_sessions": len(non_rest),
        "completed_sessions": len(done),
        "completion_pct": completion_pct,
        "critical_total": len(crit),
        "critical_done": len(crit_done),
        "critical_remaining": len(crit) - len(crit_done),
    }


# --- audit log --------------------------------------------------------------

def log_action(
    session: Session,
    user_id: int,
    *,
    action: str,
    source: str,
    session_date: date | None = None,
    detail: dict[str, Any] | None = None,
    commit: bool = True,
) -> TrainingLog:
    _require(action, ACTIONS, "action")
    _require(source, SOURCES, "source")
    entry = TrainingLog(
        user_id=user_id,
        session_date=session_date,
        action=action,
        detail=detail or {},
        source=source,
    )
    session.add(entry)
    if commit:
        session.commit()
    logger.info(
        "training_log user=%s action=%s date=%s source=%s", user_id, action, session_date, source
    )
    return entry


# --- writes -----------------------------------------------------------------

def upsert_session(
    session: Session,
    user_id: int,
    d: date,
    *,
    week: int,
    phase: str,
    session_type: str,
    title: str,
    details: str | None = None,
    duration_min: int | None = None,
    loaded: bool = False,
    priority: str = "normal",
    commit: bool = True,
) -> TrainingSession:
    """Create or update the plan session for a date (idempotent by (user, date)).

    Used by the seed script. Preserves ``status`` and user-mutation fields
    (moved_from, completed_notes, recovery_adjustment) on an existing row so a
    re-seed never wipes progress; it only refreshes the planned definition.
    """
    _require(phase, PHASES, "phase")
    _require(session_type, SESSION_TYPES, "session_type")
    _require(priority, PRIORITIES, "priority")

    row = get_session(session, user_id, d)
    if row is None:
        row = TrainingSession(user_id=user_id, date=d, status="pending")
        session.add(row)
    row.week = week
    row.phase = phase
    row.session_type = session_type
    row.title = title
    row.details = details
    row.duration_min = duration_min
    row.loaded = loaded
    row.priority = priority
    if commit:
        session.commit()
    return row


def edit_session(
    session: Session,
    user_id: int,
    d: date,
    *,
    duration_min: int | None = None,
    session_type: str | None = None,
    title: str | None = None,
    source: str = "command",
    commit: bool = True,
) -> TrainingSession | None:
    """Edit a planned session's duration / type / title. Logs the change set.
    Returns None if there's no session on that date."""
    row = get_session(session, user_id, d)
    if row is None:
        return None
    changes: dict[str, Any] = {}
    if session_type is not None:
        _require(session_type, SESSION_TYPES, "session_type")
        changes["session_type"] = {"from": row.session_type, "to": session_type}
        row.session_type = session_type
    if duration_min is not None:
        changes["duration_min"] = {"from": row.duration_min, "to": duration_min}
        row.duration_min = duration_min
    if title is not None:
        changes["title"] = {"from": row.title, "to": title}
        row.title = title
    if not changes:
        return row
    log_action(
        session, user_id, action="edited", source=source,
        session_date=d, detail={"changes": changes}, commit=False,
    )
    if commit:
        session.commit()
    return row


def mark_completed(
    session: Session,
    user_id: int,
    d: date,
    *,
    notes: str | None = None,
    source: str = "command",
    commit: bool = True,
) -> TrainingSession | None:
    """Mark a session completed as written. Returns None if there's no session on
    that date (e.g. a rest day). Records a training_log entry."""
    row = get_session(session, user_id, d)
    if row is None or row.session_type == "rest":
        return None
    row.status = "completed"
    if notes:
        row.completed_notes = notes
    log_action(
        session, user_id, action="completed", source=source,
        session_date=d, detail={"notes": notes} if notes else {}, commit=False,
    )
    if commit:
        session.commit()
    return row
