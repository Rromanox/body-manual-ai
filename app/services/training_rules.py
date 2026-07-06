"""Skip / reschedule rules for the training plan.

Core principle (spec): never stack missed workouts — consistency beats any single
session. Every mutation goes through here and writes a training_log row naming the
rule that fired, so both the /skip //move commands and the natural-language flow
produce identical state.

Outcomes are returned as dicts (never raised) so the Telegram layer can phrase the
result and, for a critical ride, present the two-button choice.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.models.training_session import TrainingSession
from app.services import training_plan as tp

logger = logging.getLogger(__name__)

_MOVABLE_TARGET_TYPES = frozenset({"rest", "z2"})  # a day we may overwrite when shifting a ride into it


def _copy_definition(from_row: TrainingSession, to_row: TrainingSession, moved_from: date) -> None:
    """Copy the planned workout definition onto ``to_row`` (its own week/phase stay)."""
    to_row.session_type = from_row.session_type
    to_row.title = from_row.title
    to_row.details = from_row.details
    to_row.duration_min = from_row.duration_min
    to_row.loaded = from_row.loaded
    to_row.priority = from_row.priority
    to_row.status = "pending"
    to_row.moved_from = moved_from


def _ensure_row(session: Session, user_id: int, d: date) -> TrainingSession:
    row = tp.get_session(session, user_id, d)
    if row is None:
        wk = tp.week_of(d) or 0
        row = TrainingSession(
            user_id=user_id, date=d, week=wk,
            phase=tp.phase_for_week(wk) if wk else "base",
            session_type="rest", title="Rest", status="pending",
        )
        session.add(row)
        session.flush()
    return row


def _relocate(
    session: Session,
    user_id: int,
    from_row: TrainingSession,
    to_date: date,
    *,
    rule: str,
    source: str,
    extra: dict[str, Any] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Move ``from_row``'s workout onto ``to_date`` (overwriting/canceling whatever
    was there). Source day becomes status=moved; target gets moved_from set."""
    from_date = from_row.date
    to_row = _ensure_row(session, user_id, to_date)
    canceled = None
    if to_row.session_type != "rest":
        canceled = {"type": to_row.session_type, "title": to_row.title}
    from_row.status = "moved"
    _copy_definition(from_row, to_row, moved_from=from_date)
    detail = {"rule": rule, "from": str(from_date), "to": str(to_date)}
    if canceled:
        detail["canceled_target"] = canceled
    if extra:
        detail.update(extra)
    tp.log_action(
        session, user_id, action="moved", source=source,
        session_date=from_date, detail=detail, commit=False,
    )
    if commit:
        session.commit()
    return {"outcome": "moved", "rule": rule, "from": from_date, "to": to_date, "canceled_target": canceled}


def _swap(
    session: Session,
    user_id: int,
    a_row: TrainingSession,
    b_row: TrainingSession,
    *,
    rule: str,
    source: str,
    commit: bool = True,
) -> dict[str, Any]:
    """Exchange the workout definitions of two dates (both marked moved_from)."""
    a_def = {
        "session_type": a_row.session_type, "title": a_row.title, "details": a_row.details,
        "duration_min": a_row.duration_min, "loaded": a_row.loaded, "priority": a_row.priority,
    }
    b_def = {
        "session_type": b_row.session_type, "title": b_row.title, "details": b_row.details,
        "duration_min": b_row.duration_min, "loaded": b_row.loaded, "priority": b_row.priority,
    }
    for row, defn, other_date in ((a_row, b_def, b_row.date), (b_row, a_def, a_row.date)):
        row.session_type = defn["session_type"]
        row.title = defn["title"]
        row.details = defn["details"]
        row.duration_min = defn["duration_min"]
        row.loaded = defn["loaded"]
        row.priority = defn["priority"]
        row.status = "pending"
        row.moved_from = other_date
    tp.log_action(
        session, user_id, action="moved", source=source, session_date=a_row.date,
        detail={"rule": rule, "swapped": [str(a_row.date), str(b_row.date)]}, commit=False,
    )
    if commit:
        session.commit()
    return {"outcome": "swapped", "rule": rule, "a": a_row.date, "b": b_row.date}


# --- skip -------------------------------------------------------------------

def skip_session(
    session: Session,
    user_id: int,
    d: date,
    *,
    reason: str | None = None,
    source: str = "command",
    commit: bool = True,
) -> dict[str, Any]:
    """Apply the skip/reschedule rules for a missed session on ``d``.

    Returns an outcome dict. A critical ride returns ``needs_choice`` and is NOT
    silently dropped — the caller must present the two options and later call
    :func:`apply_critical_choice`.
    """
    row = tp.get_session(session, user_id, d)
    if row is None or row.session_type == "rest":
        return {"outcome": "noop", "reason": "no_session"}
    if row.status in tp.DONE_STATUSES:
        return {"outcome": "noop", "reason": "already_done"}

    is_saturday = d.weekday() == 5

    # Rule 3 — critical ride: never dropped silently.
    if row.priority == "critical":
        tp.log_action(
            session, user_id, action="skipped", source=source, session_date=d,
            detail={"rule": "critical_no_silent_drop", "resolved": False, "reason": reason},
            commit=commit,
        )
        sunday = d + timedelta(days=(6 - d.weekday()) % 7 or 7) if not is_saturday else d + timedelta(days=1)
        return {
            "outcome": "needs_choice",
            "rule": "critical_no_silent_drop",
            "session_date": d,
            "options": [
                {"choice": "sunday", "to": sunday},
                {"choice": "next_saturday", "to": d + timedelta(days=7)},
            ],
        }

    # Rule 2 — missed high-priority Saturday ride: auto-shift to that week's Sunday.
    if row.priority == "high" and is_saturday:
        sunday = d + timedelta(days=1)
        sun_row = tp.get_session(session, user_id, sunday)
        sunday_movable = sun_row is None or sun_row.session_type in _MOVABLE_TARGET_TYPES
        if tp.week_of(sunday) in tp.PROTECTED_WEEKS:
            sunday_movable = False  # never add into a protected week (rule 5)
        if sunday_movable:
            return _relocate(
                session, user_id, row, sunday,
                rule="high_saturday_to_sunday", source=source,
                extra={"reason": reason}, commit=commit,
            )
        # Sunday immovable → mark skipped and log.
        row.status = "skipped"
        tp.log_action(
            session, user_id, action="skipped", source=source, session_date=d,
            detail={"rule": "high_saturday_sunday_taken", "reason": reason}, commit=commit,
        )
        return {"outcome": "skipped", "rule": "high_saturday_sunday_taken", "session_date": d}

    # Rule 1 — normal session (and any non-Saturday high): skip, no reschedule.
    row.status = "skipped"
    tp.log_action(
        session, user_id, action="skipped", source=source, session_date=d,
        detail={"rule": "normal_no_reschedule", "reason": reason}, commit=commit,
    )
    return {"outcome": "skipped", "rule": "normal_no_reschedule", "session_date": d}


def apply_critical_choice(
    session: Session,
    user_id: int,
    d: date,
    choice: str,
    *,
    source: str = "command",
    commit: bool = True,
) -> dict[str, Any]:
    """Resolve a missed critical ride: 'sunday' = shift to that week's Sunday,
    'next_saturday' = take next Saturday's slot (replacing that session)."""
    row = tp.get_session(session, user_id, d)
    if row is None or row.priority != "critical":
        return {"outcome": "noop"}
    if choice == "sunday":
        to = d + timedelta(days=(6 - d.weekday()) % 7 or 7) if d.weekday() != 5 else d + timedelta(days=1)
        rule = "critical_choice_sunday"
    elif choice == "next_saturday":
        to = d + timedelta(days=7)
        rule = "critical_choice_next_saturday"
    else:
        return {"outcome": "noop", "reason": "unknown_choice"}
    return _relocate(session, user_id, row, to, rule=rule, source=source, commit=commit)


# --- move (/move command) ---------------------------------------------------

def move_session(
    session: Session,
    user_id: int,
    from_date: date,
    to_date: date,
    *,
    source: str = "command",
    confirm_swap: bool = False,
    commit: bool = True,
) -> dict[str, Any]:
    """Reschedule a session. Target must be a rest day; otherwise the caller is
    asked to confirm a swap. Moves INTO a protected week (4, 7, 12) are rejected."""
    from_row = tp.get_session(session, user_id, from_date)
    if from_row is None or from_row.session_type == "rest":
        return {"outcome": "noop", "reason": "no_session"}
    if tp.week_of(to_date) is None:
        return {"outcome": "rejected", "reason": "outside_plan", "to": to_date}
    if tp.week_of(to_date) in tp.PROTECTED_WEEKS:
        return {
            "outcome": "rejected", "rule": "protected_week", "to": to_date,
            "week": tp.week_of(to_date),
            "reason": "recovery/taper weeks never gain sessions",
        }

    to_row = tp.get_session(session, user_id, to_date)
    target_is_rest = to_row is None or to_row.session_type == "rest"
    if not target_is_rest and not confirm_swap:
        return {
            "outcome": "needs_confirm_swap",
            "from": from_date, "to": to_date,
            "target_title": to_row.title,
        }
    if target_is_rest:
        return _relocate(
            session, user_id, from_row, to_date,
            rule="move_to_rest_day", source=source, commit=commit,
        )
    return _swap(session, user_id, from_row, to_row, rule="move_swap", source=source, commit=commit)


# --- rule 4: consecutive skips ---------------------------------------------

def consecutive_skips(session: Session, user_id: int, as_of: date) -> int:
    """Count the trailing run of skipped non-rest sessions up to and including
    ``as_of`` (rest days are transparent; a completed/moved/pending session ends
    the run)."""
    from sqlalchemy import select

    rows = list(
        session.scalars(
            select(TrainingSession)
            .where(
                TrainingSession.user_id == user_id,
                TrainingSession.date <= as_of,
                TrainingSession.session_type != "rest",
            )
            .order_by(TrainingSession.date.desc())
        ).all()
    )
    streak = 0
    for r in rows:
        if r.status == "skipped":
            streak += 1
        else:
            break
    return streak


def next_quality_session(
    session: Session, user_id: int, after: date
) -> TrainingSession | None:
    """The next hard 'quality' session (intervals/tempo) strictly after ``after``
    — the one rule 4 offers to convert to Z2 when the user says they're tired."""
    from sqlalchemy import select

    return session.scalar(
        select(TrainingSession)
        .where(
            TrainingSession.user_id == user_id,
            TrainingSession.date > after,
            TrainingSession.session_type.in_(["intervals", "tempo"]),
            TrainingSession.status == "pending",
        )
        .order_by(TrainingSession.date)
        .limit(1)
    )
