"""Deterministic weight-goal projection.

The backend computes the projection; the AI only narrates it. This exists because
the model produced impossible math ("0.4 lb/week reaches 190 in 2 weeks"). With a
precomputed projection in the payload and a prompt rule to use it verbatim, the
AI can't invent a timeline.
"""
from __future__ import annotations

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
) -> dict[str, Any] | None:
    """Project when a weight goal is reached.

    ``weekly_rate_lbs`` is the SIGNED weekly change (negative = losing weight).
    Returns a dict with a ``status`` of:
      - "reached"      already at/past the goal
      - "unavailable"  no usable rate, or moving away from the goal
      - "projected"    has estimated_weeks + estimated_date
    or None when current/goal is missing.
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
    # Loss rate = how fast weight is dropping (positive when losing).
    loss_rate = -weekly_rate_lbs

    if loss_rate <= _MIN_MEANINGFUL_RATE:
        base["reason"] = "moving_away" if loss_rate < 0 else "stalled"
        base["rate_lbs_per_week"] = round(loss_rate, 2)
        return base

    estimated_weeks = round(pounds_remaining / loss_rate, 1)
    estimated_date = today + timedelta(days=round(estimated_weeks * 7))
    return {
        "status": "projected",
        "current_lbs": current_lbs,
        "goal_lbs": goal_lbs,
        "pounds_remaining": pounds_remaining,
        "rate_lbs_per_week": round(loss_rate, 2),
        "direction": "loss",
        "estimated_weeks": estimated_weeks,
        "estimated_date": str(estimated_date),
        "short_term": bool(trend_days is not None and trend_days < _SHORT_TERM_DAYS),
    }


def format_projection(p: dict[str, Any] | None) -> str:
    """Deterministic plain-text sentence — used as the placeholder-guard fallback."""
    if not p:
        return "I don't have enough weight data to project that yet."
    status = p.get("status")
    if status == "reached":
        return f"You're already at or past {p['goal_lbs']} lbs."
    if status == "unavailable":
        if p.get("reason") == "moving_away":
            return f"Your weight is trending away from {p['goal_lbs']} lbs right now, so I can't project a date."
        return (
            f"Your weight is basically holding steady, so I can't project a date to "
            f"{p['goal_lbs']} lbs yet — {p['pounds_remaining']} lbs to go."
        )
    weeks = p["estimated_weeks"]
    line = (
        f"At about {p['rate_lbs_per_week']} lb/week, {p['goal_lbs']} lbs is roughly "
        f"{weeks} weeks away (around {p['estimated_date']}) — {p['pounds_remaining']} lbs to go."
    )
    if p.get("short_term"):
        line += " That's a short-term pace and may not hold every week."
    return line
