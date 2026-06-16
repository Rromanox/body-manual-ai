"""Observation engine: correlates check-in tags with next-day WHOOP metrics.

Recalculates from scratch on every call — idempotent, safe to call multiple
times. Each (tag, metric) pair that has enough data becomes an observation row
that feeds the Personal Operating Manual.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.daily_metric import DailyMetric
from app.models.journal_entry import JournalEntry
from app.models.observation import Observation

logger = logging.getLogger(__name__)

# Which metrics to track for each check-in tag
TRACKED_PAIRS: dict[str, list[str]] = {
    "alcohol":        ["recovery", "hrv_ms", "sleep_hours"],
    "late_meal":      ["recovery", "sleep_hours", "sleep_efficiency"],
    "high_stress":    ["recovery", "resting_heart_rate", "hrv_ms"],
    "sick":           ["recovery", "resting_heart_rate"],
    "travel":         ["recovery", "sleep_hours"],
    "hard_day":       ["recovery", "hrv_ms"],
    "late_caffeine":  ["recovery", "sleep_hours", "hrv_ms"],
    "dehydrated":     ["recovery", "hrv_ms", "resting_heart_rate"],
    "big_meal":       ["recovery", "sleep_hours", "sleep_efficiency"],
}

DESCRIPTIONS: dict[tuple[str, str], str] = {
    ("alcohol", "recovery"):                "Alcohol may lower next-day recovery",
    ("alcohol", "hrv_ms"):                  "Alcohol may suppress next-day HRV",
    ("alcohol", "sleep_hours"):             "Alcohol may disrupt sleep duration",
    ("late_meal", "recovery"):              "Late meals may reduce next-day recovery",
    ("late_meal", "sleep_hours"):           "Late meals may shorten sleep",
    ("late_meal", "sleep_efficiency"):      "Late meals may reduce sleep efficiency",
    ("high_stress", "recovery"):            "High stress may lower next-day recovery",
    ("high_stress", "resting_heart_rate"):  "High stress may elevate resting heart rate",
    ("high_stress", "hrv_ms"):              "High stress may suppress HRV",
    ("sick", "recovery"):                   "Being sick lowers recovery",
    ("sick", "resting_heart_rate"):         "Being sick elevates resting heart rate",
    ("travel", "recovery"):                 "Travel may reduce next-day recovery",
    ("travel", "sleep_hours"):              "Travel may disrupt sleep",
    ("hard_day", "recovery"):               "Hard days may lower next-day recovery",
    ("hard_day", "hrv_ms"):                 "Hard days may suppress next-day HRV",
    ("late_caffeine", "recovery"):          "Late caffeine may lower next-day recovery",
    ("late_caffeine", "sleep_hours"):       "Late caffeine may shorten sleep",
    ("late_caffeine", "hrv_ms"):            "Late caffeine may suppress next-day HRV",
    ("dehydrated", "recovery"):             "Dehydration may lower next-day recovery",
    ("dehydrated", "hrv_ms"):              "Dehydration may suppress HRV",
    ("dehydrated", "resting_heart_rate"):   "Dehydration may elevate resting heart rate",
    ("big_meal", "recovery"):               "Large meals late in the day may affect next-day recovery",
    ("big_meal", "sleep_hours"):            "Large meals may disrupt sleep duration",
    ("big_meal", "sleep_efficiency"):       "Large meals may reduce sleep efficiency",
}

# Maps the metric key used in TRACKED_PAIRS to the DailyMetric column name
METRIC_COL: dict[str, str] = {
    "recovery":           "recovery_score",
    "hrv_ms":             "hrv_ms",
    "sleep_hours":        "sleep_hours",
    "sleep_efficiency":   "sleep_efficiency",
    "resting_heart_rate": "resting_heart_rate",
}

# Don't surface a pattern until it has this many data points
MIN_OBSERVATIONS = 3

# Human-friendly labels for narrating a closed loop in the morning message
TAG_LABELS: dict[str, str] = {
    "alcohol": "drinking",
    "late_meal": "a late meal",
    "high_stress": "high stress",
    "sick": "being sick",
    "travel": "travel",
    "hard_day": "a hard day",
    "late_caffeine": "late caffeine",
    "dehydrated": "being dehydrated",
    "big_meal": "a big meal",
}
METRIC_LABELS: dict[str, str] = {
    "recovery": "recovery",
    "hrv_ms": "HRV",
    "sleep_hours": "sleep",
    "sleep_efficiency": "sleep efficiency",
    "resting_heart_rate": "resting heart rate",
}
# Metrics where a negative effect shows up as a HIGHER number (others: lower = worse)
_WORSE_IS_HIGHER = {"resting_heart_rate"}

# Loops need enough history for "your normal" to mean anything (matches flag gate)
MIN_DAYS_FOR_LOOPS = 7
# How many loops to surface at once — one per morning reads as a coach, a list reads as a nag
MAX_LOOPS = 2


def recalculate_observations(session: Session, user_id: int) -> None:
    """Recompute all pattern observations for a user from scratch and upsert results."""
    entries = session.scalars(
        select(JournalEntry).where(JournalEntry.user_id == user_id)
    ).all()

    all_metrics = session.scalars(
        select(DailyMetric).where(DailyMetric.user_id == user_id)
    ).all()
    metrics_by_date: dict[date, DailyMetric] = {m.date: m for m in all_metrics}

    # Accumulate stats per pattern_key
    stats: dict[str, dict[str, Any]] = {}

    for entry in entries:
        if not entry.tags:
            continue
        next_day = entry.date + timedelta(days=1)
        next_row = metrics_by_date.get(next_day)
        if next_row is None:
            continue

        month_start = next_day - timedelta(days=31)
        month_end = next_day - timedelta(days=1)

        for tag in entry.tags:
            for metric_key in TRACKED_PAIRS.get(tag, []):
                col_name = METRIC_COL[metric_key]
                next_val = getattr(next_row, col_name, None)
                if next_val is None:
                    continue

                baseline = _avg(metrics_by_date, col_name, month_start, month_end)
                if baseline is None:
                    continue

                pattern_key = f"{tag}_{metric_key}"
                if pattern_key not in stats:
                    stats[pattern_key] = {
                        "tag": tag,
                        "metric": metric_key,
                        "occurrences": 0,
                        "supporting": 0,
                        "first_seen": entry.date,
                        "last_seen": entry.date,
                    }
                s = stats[pattern_key]
                s["occurrences"] += 1
                if _is_supporting(metric_key, next_val, baseline):
                    s["supporting"] += 1
                s["first_seen"] = min(s["first_seen"], entry.date)
                s["last_seen"] = max(s["last_seen"], entry.date)

    for pattern_key, s in stats.items():
        if s["occurrences"] < MIN_OBSERVATIONS:
            continue

        tag, metric_key = s["tag"], s["metric"]
        description = DESCRIPTIONS.get((tag, metric_key), f"{tag} may affect {metric_key}")

        obs = session.scalar(
            select(Observation).where(
                Observation.user_id == user_id,
                Observation.pattern_key == pattern_key,
            )
        )
        if obs is None:
            obs = Observation(
                user_id=user_id,
                pattern_key=pattern_key,
                pattern_description=description,
                trigger_tag=tag,
                outcome_metric=metric_key,
            )
            session.add(obs)

        obs.occurrence_count = s["occurrences"]
        obs.supporting_count = s["supporting"]
        obs.opposing_count = s["occurrences"] - s["supporting"]
        obs.first_seen = s["first_seen"]
        obs.last_seen = s["last_seen"]
        obs.status = _compute_status(s["occurrences"], s["supporting"])

    session.commit()
    logger.info("Observation recalc complete for user %s: %s patterns evaluated", user_id, len(stats))


def build_closed_loops(
    session: Session,
    user_id: int,
    target_date: date,
    yesterday_tags: list[str] | None,
    max_loops: int = MAX_LOOPS,
) -> list[dict[str, Any]]:
    """Close the loop on what the user logged about yesterday.

    For each behavior the user tagged yesterday, check whether the matching metric
    THIS morning came in worse than their own 30-day normal — and how often that has
    lined up before. Backend computes the connection; the AI only narrates it.

    Uses the same tag→metric pairs and the same supporting-deviation test as the
    Personal Operating Manual, so an in-the-moment loop and a long-term pattern are
    the same evidence at two zoom levels. Returns at most `max_loops`, biggest
    deviation first. Empty when there's nothing worth saying.
    """
    if not yesterday_tags:
        return []

    # Same maturity gate as flags — without a real baseline "under your normal" is noise
    day_count = session.scalar(
        select(func.count(DailyMetric.id)).where(
            DailyMetric.user_id == user_id,
            DailyMetric.date <= target_date,
            (DailyMetric.recovery_score.is_not(None)) | (DailyMetric.sleep_hours.is_not(None)),
        )
    )
    if (day_count or 0) < MIN_DAYS_FOR_LOOPS:
        return []

    today_row = session.scalar(
        select(DailyMetric).where(
            DailyMetric.user_id == user_id, DailyMetric.date == target_date
        )
    )
    if today_row is None:
        return []

    yesterday = target_date - timedelta(days=1)
    month_start = target_date - timedelta(days=30)

    loops: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for tag in yesterday_tags:
        for metric_key in TRACKED_PAIRS.get(tag, []):
            key = (tag, metric_key)
            if key in seen:
                continue
            col_name = METRIC_COL[metric_key]
            col = getattr(DailyMetric, col_name)
            today_val = getattr(today_row, col_name, None)
            if today_val is None:
                continue
            # baseline excludes today so "your normal" doesn't include the day we're judging
            baseline = session.scalar(
                select(func.avg(col)).where(
                    DailyMetric.user_id == user_id,
                    DailyMetric.date >= month_start,
                    DailyMetric.date <= yesterday,
                    col.is_not(None),
                )
            )
            if baseline is None:
                continue
            baseline = float(baseline)
            # only surface a loop when the metric actually moved the wrong way
            if not _is_supporting(metric_key, today_val, baseline):
                continue
            seen.add(key)

            loop: dict[str, Any] = {
                "behavior": TAG_LABELS.get(tag, tag.replace("_", " ")),
                "metric": METRIC_LABELS.get(metric_key, metric_key),
                "description": DESCRIPTIONS.get((tag, metric_key), f"{tag} may affect {metric_key}"),
                "today": round(today_val, 1),
                "your_normal": round(baseline, 1),
                "came_in": "above" if metric_key in _WORSE_IS_HIGHER else "under",
                "_deviation": abs(today_val - baseline),
            }
            # If this pairing is already a tracked pattern, hand over the running tally
            obs = session.scalar(
                select(Observation).where(
                    Observation.user_id == user_id,
                    Observation.pattern_key == f"{tag}_{metric_key}",
                )
            )
            if obs is not None and obs.occurrence_count:
                loop["times_lined_up"] = obs.supporting_count
                loop["times_logged"] = obs.occurrence_count
            loops.append(loop)

    loops.sort(key=lambda l: l["_deviation"], reverse=True)
    for l in loops:
        l.pop("_deviation", None)
    return loops[:max_loops]


def _is_supporting(metric_key: str, value: float, baseline: float) -> bool:
    """True if the next-day value is notably worse than baseline — suggesting the tag had a negative effect."""
    if metric_key == "recovery":
        return value < baseline - 5
    if metric_key == "hrv_ms":
        return value < baseline * 0.90
    if metric_key == "sleep_hours":
        return value < baseline - 0.5
    if metric_key == "sleep_efficiency":
        return value < baseline - 5
    if metric_key == "resting_heart_rate":
        return value > baseline + 3
    return False


def _compute_status(occurrences: int, supporting: int) -> str:
    if occurrences < 4:
        return "watching"
    rate = supporting / occurrences
    if occurrences >= 10:
        if rate >= 0.60:
            return "stronger_signal"
        if rate < 0.30:
            return "weak"
    return "promising" if rate >= 0.50 else "watching"


def _avg(
    metrics_by_date: dict[date, DailyMetric],
    col_name: str,
    start: date,
    end: date,
) -> float | None:
    values = [
        getattr(m, col_name)
        for d, m in metrics_by_date.items()
        if start <= d <= end and getattr(m, col_name) is not None
    ]
    return sum(values) / len(values) if values else None
