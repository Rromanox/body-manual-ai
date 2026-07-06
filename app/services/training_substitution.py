"""Substitution engine — "I can't do today's session as planned" (/cant).

The substitution TABLE is deterministic and lives here in code (not in a prompt),
so it's testable and the LLM can only *explain* a substitution, never invent one.
``substitute`` returns the replacement; the Telegram layer narrates it.

Routing: 'feeling_beat' defers to the recovery gate (fatigue decisions stay in one
place) and 'skip' defers to the skip/reschedule rules. Critical rides are never
substituted — they return ``refused_critical`` so the caller runs the rule-3 move.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.training_log import TrainingLog
from app.services import training_plan as tp

logger = logging.getLogger(__name__)

CONSTRAINTS = frozenset({"less_time", "no_bike", "cant_leave", "feeling_beat", "skip"})
LESS_TIME_OPTIONS = (30, 45, 60)

# Prompt buttons shown when /cant is used with no stated constraint.
CONSTRAINT_BUTTONS = [
    ("Less time", "less_time"),
    ("No bike", "no_bike"),
    ("Can't leave / traveling", "cant_leave"),
    ("Feeling beat", "feeling_beat"),
    ("Skip it", "skip"),
]

_LESS_TIME_INTERVALS = {30: "2×8 min sweet spot", 45: "2×12 min sweet spot", 60: "2×15 min sweet spot"}
_GYM_SHORT = "30-min core version: squat 3×8, RDL 2×8, plank circuit, farmer carries"
_BODYWEIGHT_CIRCUIT = (
    "30–40 min bodyweight circuit: squats, lunges, step-ups, plank series, "
    "burpees 3×10 — maintenance stimulus only"
)
_NO_BIKE_INTENSITY = (
    "Off the bike: 10 min warm-up, then 4×3 min hard stair climbs or hill runs w/ "
    "2 min easy, + core circuit. Approximates the stimulus, imperfectly"
)


def _lookup(row, constraint: str, minutes: int | None) -> dict[str, Any]:
    """The deterministic table cell for (session_type × constraint)."""
    st = row.session_type

    if constraint == "less_time":
        if minutes not in LESS_TIME_OPTIONS:
            return {"kind": "needs_minutes"}
        if st == "intervals":
            return {"kind": "sub", "text": f"Keep the intensity, cut volume: {_LESS_TIME_INTERVALS[minutes]}"}
        if st == "tempo":
            return {"kind": "sub", "text": "Half the tempo block, same effort"}
        if st == "z2":
            return {"kind": "sub", "text": "All Z2 for whatever time you have — any amount counts"}
        if st == "long_ride":
            tail = ", loaded" if row.loaded else ""
            return {"kind": "sub", "text": f"Ride the time you have at endurance pace{tail}", "shortened_long_ride": True}
        if st in ("gym_a", "gym_b"):
            return {"kind": "sub", "text": _GYM_SHORT}

    if constraint == "no_bike":
        if st in ("intervals", "tempo"):
            return {"kind": "sub", "text": _NO_BIKE_INTENSITY}
        if st == "z2":
            return {"kind": "sub", "text": "45–60 min brisk incline walk or easy jog"}
        if st == "long_ride":
            return {"kind": "not_substitutable", "suggest": "move"}
        if st in ("gym_a", "gym_b"):
            return {"kind": "noop", "message": "That's a gym session — no bike needed. Do it as written."}

    if constraint == "cant_leave":
        # Any ride or gym -> the same at-home maintenance circuit.
        return {"kind": "sub", "text": _BODYWEIGHT_CIRCUIT, "maintenance_only": True}

    return {"kind": "noop", "message": "No substitution for that."}


def substitute(
    session: Session,
    user_id: int,
    d: date,
    constraint: str,
    *,
    minutes: int | None = None,
    source: str = "command",
    commit: bool = True,
) -> dict[str, Any]:
    """Apply a substitution for today's session. Returns an outcome dict; only
    'substituted' actually mutates (status -> modified, logged)."""
    if constraint not in CONSTRAINTS:
        return {"outcome": "noop", "reason": "unknown_constraint"}

    row = tp.get_session(session, user_id, d)
    if row is None or row.session_type == "rest":
        return {"outcome": "noop", "reason": "no_session"}

    # Hard constraint 1: critical rides are never substituted.
    if row.priority == "critical":
        return {"outcome": "refused_critical", "session_date": d}

    # Fatigue and outright skips are handled by their own engines.
    if constraint == "feeling_beat":
        return {"outcome": "route_to_gate", "session_date": d}
    if constraint == "skip":
        return {"outcome": "route_to_skip", "session_date": d}

    result = _lookup(row, constraint, minutes)
    if result["kind"] == "needs_minutes":
        return {"outcome": "needs_minutes", "options": list(LESS_TIME_OPTIONS), "session_date": d}
    if result["kind"] == "not_substitutable":
        return {"outcome": "not_substitutable", "suggest": result["suggest"], "session_date": d}
    if result["kind"] == "noop":
        return {"outcome": "noop", "message": result["message"], "session_date": d}

    # kind == "sub": apply.
    text = result["text"]
    maintenance_only = result.get("maintenance_only", False)
    row.status = "modified"
    detail = {
        "constraint": constraint,
        "minutes": minutes,
        "from": {"type": row.session_type, "title": row.title},
        "substitution": text,
        "maintenance_only": maintenance_only,
    }
    if result.get("shortened_long_ride"):
        detail["shortened_long_ride"] = True
    tp.log_action(
        session, user_id, action="substituted", source=source,
        session_date=d, detail=detail, commit=commit,
    )
    return {
        "outcome": "substituted",
        "text": text,
        "constraint": constraint,
        "maintenance_only": maintenance_only,
        "session_date": d,
    }


# --- hard constraint 3: too many substitutions in a week --------------------

def substitutions_this_week(session: Session, user_id: int, week: int) -> int:
    start, end = tp.week_date_range(week)
    return int(
        session.scalar(
            select(func.count(TrainingLog.id)).where(
                TrainingLog.user_id == user_id,
                TrainingLog.action == "substituted",
                TrainingLog.session_date >= start,
                TrainingLog.session_date <= end,
            )
        )
        or 0
    )


def too_many_substitutions(session: Session, user_id: int, week: int) -> bool:
    """3+ substitutions in one week — the plan may need a real edit, not more
    patches. Surfaced in the Sunday summary."""
    return substitutions_this_week(session, user_id, week) >= 3
