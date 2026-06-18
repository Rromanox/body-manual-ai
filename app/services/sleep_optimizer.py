"""Sleep pattern optimizer.

Analyzes bedtime windows vs recovery outcomes, pre-sleep behavior impact,
and strain-adjusted sleep advice — all from data already in daily_metrics
and journal_entries. No new DB tables needed.

Backend computes; the AI narrates. Functions here return pre-computed
conclusions the AI can cite directly.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.daily_metric import DailyMetric
from app.models.journal_entry import JournalEntry

MIN_NIGHTS = 3          # minimum nights per bucket before surfacing
LOOKBACK_DAYS = 90      # analysis window

# Normalized hour: 18.0 = 6 PM, 24.0 = midnight, 25.0 = 1 AM
BEDTIME_BUCKETS: list[tuple[float, float, str]] = [
    (18.0, 20.0, "before 8pm"),
    (20.0, 21.0, "8-9pm"),
    (21.0, 22.0, "9-10pm"),
    (22.0, 23.0, "10-11pm"),
    (23.0, 24.0, "11pm-midnight"),
    (24.0, 25.0, "midnight-1am"),
    (25.0, 26.0, "1-2am"),
    (26.0, 30.0, "after 2am"),
]

HIGH_STRAIN_THRESHOLD = 14.0

_DISRUPTORS = frozenset({
    "alcohol", "late_meal", "high_stress", "sick", "travel",
    "hard_day", "late_caffeine", "dehydrated", "big_meal",
})
_HELPERS = frozenset({"early_dinner", "early_bedtime", "well_hydrated", "meditated"})


def _norm_hour(hhmm: str) -> float | None:
    """'HH:MM' → normalized float (18.0 = 6pm, 24.0 = midnight, 25.0 = 1am)."""
    try:
        h, m = int(hhmm[:2]), int(hhmm[3:5])
    except (ValueError, IndexError):
        return None
    t = h + m / 60.0
    if t < 12.0:
        t += 24.0  # 0-12 AM = past midnight
    return t


def _bucket_label(norm_hour: float) -> str | None:
    for lo, hi, label in BEDTIME_BUCKETS:
        if lo <= norm_hour < hi:
            return label
    return None


def get_bedtime_recovery_profile(
    session: Session,
    user_id: int,
    lookback_days: int = LOOKBACK_DAYS,
) -> list[dict[str, Any]]:
    """Bedtime windows with average recovery, earliest to latest.

    Only includes windows with at least MIN_NIGHTS nights of data.
    """
    cutoff = date.today() - timedelta(days=lookback_days)
    rows = session.scalars(
        select(DailyMetric).where(
            DailyMetric.user_id == user_id,
            DailyMetric.date >= cutoff,
            DailyMetric.sleep_start_local.is_not(None),
            DailyMetric.recovery_score.is_not(None),
        )
    ).all()

    bucket_recovery: dict[str, list[float]] = {}
    bucket_hrv: dict[str, list[float]] = {}
    for r in rows:
        nh = _norm_hour(r.sleep_start_local)
        if nh is None:
            continue
        label = _bucket_label(nh)
        if label is None:
            continue
        bucket_recovery.setdefault(label, []).append(r.recovery_score)
        if r.hrv_ms is not None:
            bucket_hrv.setdefault(label, []).append(r.hrv_ms)

    result = []
    for _, _, label in BEDTIME_BUCKETS:
        scores = bucket_recovery.get(label, [])
        if len(scores) < MIN_NIGHTS:
            continue
        hrv_scores = bucket_hrv.get(label, [])
        entry: dict[str, Any] = {
            "window": label,
            "nights": len(scores),
            "avg_recovery": round(sum(scores) / len(scores), 1),
        }
        if hrv_scores:
            entry["avg_hrv_ms"] = round(sum(hrv_scores) / len(hrv_scores), 1)
        result.append(entry)
    return result


def get_pre_sleep_factor_impact(
    session: Session,
    user_id: int,
    lookback_days: int = LOOKBACK_DAYS,
) -> list[dict[str, Any]]:
    """Per-tag average recovery on the day after, compared to no-tag baseline.

    Only includes tags with at least MIN_NIGHTS logged nights.
    Sorted by delta (most negative disruptors first, best helpers last).
    """
    cutoff = date.today() - timedelta(days=lookback_days)

    metrics = session.scalars(
        select(DailyMetric).where(
            DailyMetric.user_id == user_id,
            DailyMetric.date >= cutoff,
            DailyMetric.recovery_score.is_not(None),
        )
    ).all()
    metrics_by_date: dict[date, float] = {m.date: m.recovery_score for m in metrics}

    entries = session.scalars(
        select(JournalEntry).where(
            JournalEntry.user_id == user_id,
            JournalEntry.date >= cutoff,
        )
    ).all()
    entries_by_date: dict[date, JournalEntry] = {e.date: e for e in entries}

    tag_scores: dict[str, list[float]] = {}
    no_tag_scores: list[float] = []

    for m in metrics:
        yesterday = m.date - timedelta(days=1)
        entry = entries_by_date.get(yesterday)
        tags = list(entry.tags or []) if entry else []
        if tags:
            for tag in tags:
                tag_scores.setdefault(tag, []).append(m.recovery_score)
        else:
            no_tag_scores.append(m.recovery_score)

    baseline = sum(no_tag_scores) / len(no_tag_scores) if no_tag_scores else None

    results = []
    for tag, scores in tag_scores.items():
        if len(scores) < MIN_NIGHTS:
            continue
        avg = sum(scores) / len(scores)
        delta = round(avg - baseline, 1) if baseline is not None else None
        results.append({
            "tag": tag,
            "nights_logged": len(scores),
            "avg_recovery": round(avg, 1),
            "delta_vs_no_tag": delta,
            "type": "helper" if tag in _HELPERS else ("disruptor" if tag in _DISRUPTORS else "neutral"),
        })

    results.sort(key=lambda x: (x.get("delta_vs_no_tag") or 0))
    return results


def get_strain_sleep_advice(
    session: Session,
    user_id: int,
    lookback_days: int = LOOKBACK_DAYS,
) -> dict[str, Any] | None:
    """Optimal bedtime separately for high-strain vs normal days.

    Useful because high-strain days often need an earlier bedtime to
    achieve the same recovery as a normal day.
    """
    cutoff = date.today() - timedelta(days=lookback_days)
    rows = session.scalars(
        select(DailyMetric).where(
            DailyMetric.user_id == user_id,
            DailyMetric.date >= cutoff,
            DailyMetric.sleep_start_local.is_not(None),
            DailyMetric.recovery_score.is_not(None),
        )
    ).all()

    high_buckets: dict[str, list[float]] = {}
    normal_buckets: dict[str, list[float]] = {}

    for r in rows:
        nh = _norm_hour(r.sleep_start_local)
        if nh is None:
            continue
        label = _bucket_label(nh)
        if label is None:
            continue
        target = high_buckets if (r.strain or 0) >= HIGH_STRAIN_THRESHOLD else normal_buckets
        target.setdefault(label, []).append(r.recovery_score)

    def _best(buckets: dict) -> dict | None:
        candidates = [
            {"window": lbl, "avg_recovery": round(sum(s) / len(s), 1), "nights": len(s)}
            for lbl, s in buckets.items()
            if len(s) >= MIN_NIGHTS
        ]
        return max(candidates, key=lambda x: x["avg_recovery"]) if candidates else None

    best_high = _best(high_buckets)
    best_normal = _best(normal_buckets)
    if best_high is None and best_normal is None:
        return None

    return {
        "high_strain_optimal": best_high,
        "normal_day_optimal": best_normal,
        "note": (
            "On high-strain days you need an earlier bedtime to hit the same recovery"
            if best_high and best_normal and best_high["window"] != best_normal["window"]
            else None
        ),
    }


def get_bedtime_deviation(
    session: Session,
    user_id: int,
    target_date: date,
) -> dict[str, Any] | None:
    """Was last night's bedtime outside the optimal window?

    Returns a deviation dict the morning message can use to flag a late bedtime.
    Returns None when bedtime was on-target or there's not enough data yet.
    """
    profile = get_bedtime_recovery_profile(session, user_id)
    if not profile:
        return None

    optimal = max(profile, key=lambda b: b["avg_recovery"])

    today_row = session.scalar(
        select(DailyMetric).where(
            DailyMetric.user_id == user_id,
            DailyMetric.date == target_date,
        )
    )
    if today_row is None or not today_row.sleep_start_local:
        return None

    nh = _norm_hour(today_row.sleep_start_local)
    if nh is None:
        return None

    actual_label = _bucket_label(nh)
    if actual_label is None or actual_label == optimal["window"]:
        return None  # on target — nothing to flag

    actual_bucket = next((b for b in profile if b["window"] == actual_label), None)

    return {
        "actual_bedtime": today_row.sleep_start_local,
        "actual_window": actual_label,
        "optimal_window": optimal["window"],
        "optimal_avg_recovery": optimal["avg_recovery"],
        "actual_avg_recovery": actual_bucket["avg_recovery"] if actual_bucket else None,
    }


def build_sleep_insights(
    session: Session,
    user_id: int,
) -> dict[str, Any] | None:
    """Full sleep insight payload for Q&A context.

    Includes bedtime profile, optimal window, pre-sleep factor impact,
    and strain-adjusted advice. Returns None when there's not enough data yet.
    """
    profile = get_bedtime_recovery_profile(session, user_id)
    if not profile:
        return None

    optimal = max(profile, key=lambda b: b["avg_recovery"])
    factors = get_pre_sleep_factor_impact(session, user_id)
    strain_advice = get_strain_sleep_advice(session, user_id)

    return {
        "bedtime_profile": profile,
        "optimal_bedtime": optimal,
        "pre_sleep_factors": factors,
        "strain_advice": strain_advice,
    }
