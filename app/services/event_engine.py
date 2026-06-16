"""Free-text event logging (COACH_FEEL.md flagship): rollup + consequence follow-up.

Events are a richer, timestamped *source* for the same tag vocabulary the
observation engine already consumes — not a parallel system. `apply_event_to_tags`
converts a logged event into the existing checkbox tags (alcohol, late_meal, ...)
so `observation_engine.build_closed_loops` / `recalculate_observations` keep
working completely unchanged: one correlation engine, two input methods.

`enrich_closed_loops_with_meal_gap` adds the one piece of precision the design
doc explicitly calls out: the meal-to-bed gap needs the night's *real* sleep
onset timestamp, not the date-less `sleep_start_local` string. That timestamp
only exists in `daily_metrics.raw_whoop_json["sleeps"]`, so this re-derives the
"primary sleep" the same way metrics_normalizer does, rather than guessing.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.daily_metric import DailyMetric
from app.models.event import Event
from app.models.journal_entry import JournalEntry
from app.services.metrics_normalizer import _sleep_duration_milli, parse_whoop_timestamp

logger = logging.getLogger(__name__)

EVENT_TYPES = {"meal", "alcohol", "caffeine", "stress", "exercise", "sleep_problem", "note"}

# Coarse, immediate tag mapping — applied the moment an event is logged, before
# any sleep data exists to judge it against. "Late" thresholds match what a
# user tapping the check-in checkboxes would mean by the same tag.
_LATE_MEAL_HOUR = 20
_LATE_CAFFEINE_HOUR = 14


def apply_event_to_tags(session: Session, user_id: int, event_type: str, occurred_at: datetime) -> None:
    """Roll a logged event into journal_entries.tags for its behavior date."""
    tag: str | None = None
    if event_type == "alcohol":
        tag = "alcohol"
    elif event_type == "stress":
        tag = "high_stress"
    elif event_type == "meal" and occurred_at.hour >= _LATE_MEAL_HOUR:
        tag = "late_meal"
    elif event_type == "caffeine" and occurred_at.hour >= _LATE_CAFFEINE_HOUR:
        tag = "late_caffeine"
    if tag is None:
        return

    behavior_date = occurred_at.date()
    entry = session.scalar(
        select(JournalEntry).where(JournalEntry.user_id == user_id, JournalEntry.date == behavior_date)
    )
    if entry is None:
        entry = JournalEntry(user_id=user_id, date=behavior_date, tags=[])
        session.add(entry)
    current = list(entry.tags or [])
    if tag not in current:
        current.append(tag)
        entry.tags = current
    session.commit()


def primary_sleep_onset(today_metric_row: DailyMetric) -> datetime | None:
    """The real UTC onset timestamp of the night's primary sleep.

    Mirrors metrics_normalizer's "longest non-nap SCORED sleep wins" selection,
    re-applied against the row's stored raw_whoop_json since the normalized
    sleep_start_local column has no date attached to it.
    """
    raw = today_metric_row.raw_whoop_json or {}
    sleeps = raw.get("sleeps") or []
    best: dict[str, Any] | None = None
    best_milli = -1
    for sleep in sleeps:
        if sleep.get("nap") or sleep.get("score_state") != "SCORED" or not sleep.get("start"):
            continue
        score = sleep.get("score") or {}
        milli = _sleep_duration_milli(sleep, score)
        if milli > best_milli:
            best_milli = milli
            best = sleep
    if best is None:
        return None
    return parse_whoop_timestamp(best["start"])


def enrich_closed_loops_with_meal_gap(
    session: Session,
    user_id: int,
    target_date: date,
    closed_loops: list[dict[str, Any]],
    today_metric_row: DailyMetric | None,
) -> None:
    """Mutates closed_loops in place: attaches a precise hours_before_bed to a
    late_meal loop when a matching logged meal event exists for last night."""
    if not closed_loops or today_metric_row is None:
        return
    if not any(l.get("behavior") == "a late meal" for l in closed_loops):
        return

    onset = primary_sleep_onset(today_metric_row)
    if onset is None:
        return

    yesterday = target_date - timedelta(days=1)
    meal_event = session.scalar(
        select(Event)
        .where(
            Event.user_id == user_id,
            Event.local_date == yesterday,
            Event.event_type == "meal",
        )
        .order_by(Event.occurred_at.desc())
    )
    if meal_event is None:
        return

    gap_hours = (onset - meal_event.occurred_at).total_seconds() / 3600
    if gap_hours < 0:
        return  # meal logged after sleep onset — clock mismatch, don't report nonsense

    for loop in closed_loops:
        if loop.get("behavior") == "a late meal":
            loop["hours_before_bed"] = round(gap_hours, 1)
