"""Deterministic weight-goal projection (Accuracy Guard 2).

The backend computes the projection AND the calendar date; the AI only narrates
them. This exists because the model produced impossible math and invented dates
("0.4 lb/week reaches 190 in 2 weeks"; "1.6 weeks from Jun 19 = July 3"). The
payload carries estimated_weeks/estimated_days/estimated_date and the AI is told
to use them verbatim. Question-aware: parses the target weight and hypotheticals
("if my weight stalls", "what if I lose 1 lb/week") so the projection matches the
actual question.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

# Below this weekly pace we still project, but flag it as effectively stalled.
_MIN_MEANINGFUL_RATE = 0.05
# Trends built from fewer than this many days of readings are "short-term".
_SHORT_TERM_DAYS = 10


def project_weight(
    current_lbs: float | None,
    goal_lbs: float | None,
    weekly_rate_lbs: float | None,
    today: date,
    *,
    trend_days: int | None = None,
    rate_window_days: int | None = None,
    rate_method: str | None = None,
) -> dict[str, Any] | None:
    """Project when a weight goal is reached.

    ``weekly_rate_lbs`` is the SIGNED weekly change (negative = losing weight).
    Returns a dict with a ``status`` of "reached" / "unavailable" / "projected",
    or None when current/goal is missing. estimated_days and estimated_date are
    computed here — callers must not recompute the date.
    """
    if current_lbs is None or goal_lbs is None:
        return None

    current_lbs = round(float(current_lbs), 1)
    goal_lbs = round(float(goal_lbs), 1)

    # goal_weight here is always a weight-LOSS target — at or below it = reached.
    if current_lbs <= goal_lbs:
        return {"status": "reached", "current_lbs": current_lbs, "goal_lbs": goal_lbs, "pounds_remaining": 0.0}

    pounds_remaining = round(current_lbs - goal_lbs, 1)
    base = {
        "status": "unavailable",
        "current_lbs": current_lbs,
        "goal_lbs": goal_lbs,
        "pounds_remaining": pounds_remaining,
    }

    if weekly_rate_lbs is None:
        base["reason"] = "no_rate"
        return base

    weekly_rate_lbs = round(float(weekly_rate_lbs), 2)
    loss_rate = -weekly_rate_lbs  # positive when losing

    if loss_rate <= _MIN_MEANINGFUL_RATE:
        base["reason"] = "moving_away" if loss_rate < 0 else "stalled"
        base["rate_lbs_per_week"] = round(loss_rate, 2)
        return base

    exact_weeks = pounds_remaining / loss_rate
    estimated_days = round(exact_weeks * 7)
    estimated_date = today + timedelta(days=estimated_days)
    result = {
        "status": "projected",
        "current_lbs": current_lbs,
        "goal_lbs": goal_lbs,
        "pounds_remaining": pounds_remaining,
        "rate_lbs_per_week": round(loss_rate, 2),
        "selected_rate_lbs_per_week": round(loss_rate, 2),
        "direction": "loss",
        "estimated_weeks": round(exact_weeks, 1),
        "estimated_days": estimated_days,
        "estimated_date": str(estimated_date),
        "short_term": bool(
            (trend_days is not None and trend_days < _SHORT_TERM_DAYS)
            or (rate_window_days is not None and rate_window_days < _SHORT_TERM_DAYS)
        ),
    }
    if rate_window_days is not None:
        result["selected_rate_window_days"] = rate_window_days
    if rate_method is not None:
        result["selected_rate_method"] = rate_method
    return result


def stall_projection(current_lbs: float, goal_lbs: float) -> dict[str, Any]:
    """Hypothetical: weight stalls (rate 0) -> no projected date."""
    current_lbs, goal_lbs = round(float(current_lbs), 1), round(float(goal_lbs), 1)
    if current_lbs <= goal_lbs:
        return {"status": "reached", "current_lbs": current_lbs, "goal_lbs": goal_lbs, "pounds_remaining": 0.0}
    return {
        "status": "unavailable",
        "reason": "stall_hypothetical",
        "current_lbs": current_lbs,
        "goal_lbs": goal_lbs,
        "pounds_remaining": round(current_lbs - goal_lbs, 1),
        "rate_lbs_per_week": 0.0,
    }


# --- question parsing -------------------------------------------------------

_TARGET_RE = re.compile(r"\b(?:hit|reach|get to|down to|to)\s+(\d{2,3}(?:\.\d)?)\s*(?:lbs?|pounds?)?\b", re.IGNORECASE)
_STALL_RE = re.compile(
    r"\b(stalls?|plateaus?|stop(s|ped)? losing|stays? the same|stay the same|"
    r"maintain|don'?t lose (any ?more)?|do not lose|no(?:t)? los(e|ing)|weight stays)\b",
    re.IGNORECASE,
)
_RATE_RE = re.compile(r"\b(?:lose|losing|drop|dropping)\s+(\d+(?:\.\d+)?)\s*(?:lbs?|pounds?)\s*(?:a|per|/)\s*week", re.IGNORECASE)


def parse_target_weight(question: str) -> float | None:
    m = _TARGET_RE.search(question or "")
    return float(m.group(1)) if m else None


def detect_hypothetical(question: str) -> dict[str, Any] | None:
    """Return {"type": "stall"} or {"type": "rate", "rate": <lbs/wk>} or None."""
    q = question or ""
    m = _RATE_RE.search(q)
    if m:
        return {"type": "rate", "rate": float(m.group(1))}
    if _STALL_RE.search(q):
        return {"type": "stall"}
    return None


def projection_for_question(
    question: str,
    current_lbs: float | None,
    goal_lbs: float | None,
    weekly_rate_lbs: float | None,
    today: date,
    *,
    trend_days: int | None = None,
    rate_window_days: int | None = None,
    rate_method: str | None = None,
) -> dict[str, Any] | None:
    """Question-aware projection: honor a target weight named in the question and
    any hypothetical (stall / explicit rate). Falls back to the user's goal weight
    and selected trend rate (whose window/method are echoed for transparency)."""
    if current_lbs is None:
        return None
    target = parse_target_weight(question)
    if target is None:
        target = goal_lbs
    if target is None:
        return None

    hypo = detect_hypothetical(question)
    if hypo is not None and current_lbs > target:
        if hypo["type"] == "stall":
            return stall_projection(current_lbs, target)
        if hypo["type"] == "rate":
            return project_weight(
                current_lbs, target, -hypo["rate"], today, rate_method="user_hypothetical"
            )
    return project_weight(
        current_lbs, target, weekly_rate_lbs, today,
        trend_days=trend_days, rate_window_days=rate_window_days, rate_method=rate_method,
    )


def format_projection(p: dict[str, Any] | None) -> str:
    """Deterministic plain-text sentence — used as the guard fallback."""
    if not p:
        return "I don't have enough weight data to project that yet."
    status = p.get("status")
    if status == "reached":
        return f"You're already at or past {p['goal_lbs']} lbs."
    if status == "unavailable":
        if p.get("reason") == "stall_hypothetical":
            return (
                f"If your weight truly stalls, there's no projected date to reach {p['goal_lbs']} lbs — "
                f"the rate would be 0. You'd need weight loss to resume. ({p['pounds_remaining']} lbs to go.)"
            )
        if p.get("reason") == "moving_away":
            return f"Your weight is trending away from {p['goal_lbs']} lbs right now, so I can't project a date."
        return (
            f"Your weight is basically holding steady, so I can't project a date to "
            f"{p['goal_lbs']} lbs yet — {p['pounds_remaining']} lbs to go."
        )
    line = (
        f"At about {p['rate_lbs_per_week']} lb/week, {p['goal_lbs']} lbs is roughly "
        f"{p['estimated_weeks']} weeks away (around {p['estimated_date']}) — {p['pounds_remaining']} lbs to go."
    )
    if p.get("short_term"):
        line += " That's a short-term pace and may not hold every week."
    return line
