"""Baselines, deltas, flags, and backend-enforced safety triggers.

The backend calculates; the AI explains. Everything here is computed in
SQL/Python and handed to the payload builder as finished conclusions.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import InstrumentedAttribute

from app.models.daily_metric import DailyMetric

# Flag thresholds, all relative to the user's own 30-day baseline
RECOVERY_LOW_DELTA = 10.0  # points
SLEEP_SHORT_HOURS = 1.0
RHR_ELEVATED_BPM = 5.0
HRV_BELOW_RATIO = 0.85
MIN_DAYS_FOR_FLAGS = 7  # below this, baselines aren't meaningful — no flags

# WHOOP strain bands used to classify yesterday's strain
STRAIN_MODERATE = 10.0
STRAIN_HIGH = 14.0

# Backend-enforced safety triggers (SPEC §Safety Rules). The two that are
# computable from WHOOP data alone in Week 1.
RECOVERY_VERY_LOW = 33.0
RHR_STREAK_DAYS = 5
RECOVERY_STREAK_DAYS = 7

# Weight trend constants (Withings data)
KG_TO_LBS = 2.20462
WATER_SPIKE_LBS = 2.5         # overnight Δ suggesting water weight or measurement noise
WEIGHT_TREND_FLAG_LBS = 0.5   # weekly rate (absolute) before flagging a trend
WEIGHT_LOSS_SAFETY_LBS = 3.0  # lbs/week → safety trigger for rapid loss

SAFETY_TRIGGER_DESCRIPTIONS = {
    "rhr_elevated_5d": "your resting heart rate has been above your normal for 5+ days in a row",
    "recovery_low_7d": "your recovery has been very low for 7+ days in a row",
    "weight_loss_rapid": "your weight has dropped more than 3 lbs per week on average over the past two weeks — rapid unexplained loss is worth checking on",
}


@dataclass
class MetricSummary:
    today: float | None
    baseline_7d: float | None
    baseline_30d: float | None
    flag: str | None


@dataclass
class WeightTrend:
    overnight_change_lbs: float | None  # positive = gained, negative = lost
    weekly_trend_lbs: float | None      # positive = gaining per week, negative = losing
    flag: str | None                    # "spike_likely_water" | "declining" | "gaining"
    current_weight_lbs: float | None = None   # most recent reading
    projected_2w_lbs: float | None = None     # current + (weekly_trend * 2)


@dataclass
class DailySnapshot:
    target_date: date
    recovery: MetricSummary
    sleep_hours: MetricSummary
    resting_hr: MetricSummary
    hrv: MetricSummary
    yesterday_strain: str | None
    yesterday_workout_count: int | None
    yesterday_workout_minutes: float | None
    data_days_available: int
    data_maturity: str
    safety_triggers: list[str]
    weight_trend: WeightTrend | None = None
    # Yesterday's actual morning numbers, for day-over-day continuity
    yesterday_recovery: float | None = None
    yesterday_sleep_hours: float | None = None
    yesterday_resting_hr: float | None = None
    yesterday_hrv: float | None = None
    tag_streaks: list[dict] | None = None   # tags logged N days in a row (N>=2)
    creatine_streak: int = 0                # consecutive days creatine was taken
    bedtime_deviation: dict | None = None   # last night's bedtime vs optimal window
    training_intensity: str | None = None   # "push" | "moderate" | "easy" — pre-computed from recovery + strain
    sleep_debt: dict | None = None          # weekly sleep deficit vs user's own optimal
    wake_consistency: dict | None = None   # avg wake time + std deviation over last 21 days
    hrv_trend: dict | None = None          # 30d HRV avg now vs 60-90d ago (fitness progress)
    readiness_streak: int = 0             # consecutive days recovery >= 67 (WHOOP green)
    workout_effect: dict | None = None   # recovery after workout vs rest days
    weight_velocity: dict | None = None  # weight change rate now vs prior 4 weeks (90d lookback)
    weight_velocity: dict | None = None  # body comp velocity: rate now vs 4 weeks ago


def build_daily_snapshot(session: Session, user_id: int, target_date: date) -> DailySnapshot:
    today_row = _row_for(session, user_id, target_date)
    yesterday_row = _row_for(session, user_id, target_date - timedelta(days=1))
    data_days = _data_days_available(session, user_id, target_date)

    recovery = _summarize(session, user_id, target_date, DailyMetric.recovery_score, today_row)
    sleep_hours = _summarize(session, user_id, target_date, DailyMetric.sleep_hours, today_row)
    resting_hr = _summarize(session, user_id, target_date, DailyMetric.resting_heart_rate, today_row)
    hrv = _summarize(session, user_id, target_date, DailyMetric.hrv_ms, today_row)

    if data_days >= MIN_DAYS_FOR_FLAGS:
        recovery.flag = _flag_low_high(recovery, RECOVERY_LOW_DELTA, "low_vs_baseline", "high_vs_baseline")
        sleep_hours.flag = _flag_low_high(
            sleep_hours, SLEEP_SHORT_HOURS, "short_vs_baseline", "long_vs_baseline"
        )
        resting_hr.flag = _flag_rhr(resting_hr)
        hrv.flag = _flag_hrv(hrv)

    weight_trend = _build_weight_trend(session, user_id, target_date)
    yesterday_strain_label = _classify_strain(yesterday_row.strain if yesterday_row else None)
    return DailySnapshot(
        target_date=target_date,
        recovery=recovery,
        sleep_hours=sleep_hours,
        resting_hr=resting_hr,
        hrv=hrv,
        yesterday_strain=yesterday_strain_label,
        yesterday_workout_count=yesterday_row.workout_count if yesterday_row else None,
        yesterday_workout_minutes=yesterday_row.total_workout_minutes if yesterday_row else None,
        data_days_available=data_days,
        data_maturity=_maturity(data_days),
        safety_triggers=_safety_triggers(session, user_id, target_date, resting_hr.baseline_30d, weight_trend),
        weight_trend=weight_trend,
        yesterday_recovery=yesterday_row.recovery_score if yesterday_row else None,
        yesterday_sleep_hours=yesterday_row.sleep_hours if yesterday_row else None,
        yesterday_resting_hr=yesterday_row.resting_heart_rate if yesterday_row else None,
        yesterday_hrv=yesterday_row.hrv_ms if yesterday_row else None,
        tag_streaks=_get_tag_streaks(session, user_id, target_date) or None,
        creatine_streak=_get_supplement_streak(session, user_id, target_date),
        bedtime_deviation=_get_bedtime_deviation(session, user_id, target_date),
        training_intensity=_compute_training_intensity(recovery.today, yesterday_strain_label),
        sleep_debt=_get_sleep_debt(session, user_id, target_date),
        wake_consistency=_get_wake_consistency(session, user_id),
        hrv_trend=get_hrv_baseline_trend(session, user_id, target_date),
        readiness_streak=_get_readiness_streak(session, user_id, target_date),
        workout_effect=get_workout_recovery_effect(session, user_id, target_date),
        weight_velocity=get_weight_velocity(session, user_id, target_date),
    )


def get_weight_velocity(
    session: Session,
    user_id: int,
    target_date: date,
) -> dict | None:
    """Compare weight change rate this 4 weeks vs the previous 4 weeks.

    Detects whether progress is accelerating, steady, decelerating, or stalled.
    Only returns a result when both windows have at least 5 weight readings.
    """
    KG_TO_LBS = 2.20462

    def _period_avg(start: date, end: date) -> tuple[float | None, int]:
        rows = session.scalars(
            select(DailyMetric).where(
                DailyMetric.user_id == user_id,
                DailyMetric.date >= start,
                DailyMetric.date <= end,
                DailyMetric.weight.is_not(None),
            )
        ).all()
        if len(rows) < 5:
            return None, 0
        return sum(r.weight for r in rows) / len(rows), len(rows)

    current_avg, current_n = _period_avg(target_date - timedelta(days=28), target_date)
    prev_avg, prev_n = _period_avg(target_date - timedelta(days=56), target_date - timedelta(days=29))

    if current_avg is None or prev_avg is None:
        return None

    # Weekly rate for each period (lbs/week)
    current_weekly = round((current_avg - prev_avg) / 4 * KG_TO_LBS, 2)
    prev_weekly_start_avg, prev_n2 = _period_avg(
        target_date - timedelta(days=84), target_date - timedelta(days=57)
    )
    if prev_n2 < 5 or prev_weekly_start_avg is None:
        # Can't compute prior period rate — just return the current rate
        return {
            "current_weekly_rate_lbs": current_weekly,
            "weeks_analyzed": 4,
        } if abs(current_weekly) > 0.1 else None

    prev_weekly = round((prev_avg - prev_weekly_start_avg) / 4 * KG_TO_LBS, 2)
    velocity_change = round(current_weekly - prev_weekly, 2)

    # Classify: for weight_loss users negative current_weekly is progress;
    # a less-negative current vs prev means decelerating.
    abs_change = abs(velocity_change)
    if abs_change < 0.1:
        status = "steady"
    elif abs(current_weekly) < 0.15:
        status = "stalled"
    elif (current_weekly < 0 and prev_weekly < 0 and current_weekly < prev_weekly):
        status = "accelerating"
    elif (current_weekly > 0 and prev_weekly > 0 and current_weekly > prev_weekly):
        status = "accelerating"
    else:
        status = "decelerating"

    return {
        "current_4w_weekly_rate_lbs": current_weekly,
        "previous_4w_weekly_rate_lbs": prev_weekly,
        "velocity_change_lbs": velocity_change,
        "status": status,
    }


def get_workout_recovery_effect(
    session: Session,
    user_id: int,
    target_date: date,
    lookback_days: int = 90,
) -> dict | None:
    """Compare recovery the day after workout days vs rest days.

    Only returns a result when >= 10 data points in each category AND the
    difference is >= 5 points — anything smaller is noise.
    """
    cutoff = target_date - timedelta(days=lookback_days)
    rows = session.scalars(
        select(DailyMetric)
        .where(
            DailyMetric.user_id == user_id,
            DailyMetric.date >= cutoff,
            DailyMetric.date <= target_date,
        )
        .order_by(DailyMetric.date)
    ).all()

    by_date: dict[date, DailyMetric] = {r.date: r for r in rows}

    workout_next: list[float] = []
    rest_next: list[float] = []
    for r in rows:
        next_row = by_date.get(r.date + timedelta(days=1))
        if next_row is None or next_row.recovery_score is None:
            continue
        if r.workout_count and r.workout_count > 0:
            workout_next.append(next_row.recovery_score)
        else:
            rest_next.append(next_row.recovery_score)

    if len(workout_next) < 10 or len(rest_next) < 10:
        return None

    workout_avg = sum(workout_next) / len(workout_next)
    rest_avg = sum(rest_next) / len(rest_next)
    diff = round(workout_avg - rest_avg, 1)
    if abs(diff) < 5.0:
        return None

    return {
        "after_workout_avg_recovery": round(workout_avg, 1),
        "after_rest_avg_recovery": round(rest_avg, 1),
        "difference": diff,
        "workout_days_analyzed": len(workout_next),
        "rest_days_analyzed": len(rest_next),
        "pattern": "lower_after_workout" if diff < 0 else "higher_after_workout",
    }


def _get_readiness_streak(session: Session, user_id: int, target_date: date) -> int:
    """Consecutive days (up to and including today) where recovery >= 67.

    Returns 0 when streak < 3 — not worth surfacing until there's a real pattern.
    """
    rows = session.scalars(
        select(DailyMetric)
        .where(
            DailyMetric.user_id == user_id,
            DailyMetric.date <= target_date,
            DailyMetric.recovery_score.is_not(None),
        )
        .order_by(DailyMetric.date.desc())
        .limit(90)
    ).all()

    streak = 0
    for r in rows:
        if r.recovery_score >= 67:
            streak += 1
        else:
            break

    return streak if streak >= 3 else 0


def _get_wake_consistency(session: Session, user_id: int) -> dict | None:
    from app.services.sleep_optimizer import get_wake_time_analysis
    result = get_wake_time_analysis(session, user_id)
    # Only surface when there's a problem worth flagging
    if result and result.get("consistency") in ("somewhat_inconsistent", "inconsistent"):
        return result
    return None


def _get_sleep_debt(session: Session, user_id: int, target_date: date) -> dict | None:
    from app.services.sleep_optimizer import calculate_sleep_debt
    return calculate_sleep_debt(session, user_id, target_date)


def _compute_training_intensity(
    recovery_score: float | None,
    yesterday_strain: str | None,
) -> str | None:
    """'push' / 'moderate' / 'easy' — computed from today's recovery and yesterday's strain.

    The AI narrates this; it never derives it itself.
    """
    if recovery_score is None:
        return None
    if recovery_score >= 67:
        if yesterday_strain == "high":
            return "moderate"  # body still absorbing high-strain load
        return "push"
    if recovery_score >= 34:
        return "moderate"
    return "easy"


def get_hrv_baseline_trend(
    session: Session,
    user_id: int,
    target_date: date,
) -> dict | None:
    """Compare current 30-day HRV average to 30-day average from 60-90 days ago.

    Only returns a result when:
    - At least 60 days of data exist
    - Both windows have at least 10 readings
    - The change is >= 5% (meaningful signal, not noise)
    """
    data_days = _data_days_available(session, user_id, target_date)
    if data_days < 60:
        return None

    current_end = target_date - timedelta(days=1)
    current_start = target_date - timedelta(days=30)
    hist_end = target_date - timedelta(days=61)
    hist_start = target_date - timedelta(days=90)

    from sqlalchemy import func as sqlfunc
    current_avg = session.scalar(
        select(sqlfunc.avg(DailyMetric.hrv_ms)).where(
            DailyMetric.user_id == user_id,
            DailyMetric.date >= current_start,
            DailyMetric.date <= current_end,
            DailyMetric.hrv_ms.is_not(None),
        )
    )
    hist_avg = session.scalar(
        select(sqlfunc.avg(DailyMetric.hrv_ms)).where(
            DailyMetric.user_id == user_id,
            DailyMetric.date >= hist_start,
            DailyMetric.date <= hist_end,
            DailyMetric.hrv_ms.is_not(None),
        )
    )
    # Require enough readings in each window
    current_count = session.scalar(
        select(sqlfunc.count(DailyMetric.hrv_ms)).where(
            DailyMetric.user_id == user_id,
            DailyMetric.date >= current_start,
            DailyMetric.date <= current_end,
            DailyMetric.hrv_ms.is_not(None),
        )
    )
    hist_count = session.scalar(
        select(sqlfunc.count(DailyMetric.hrv_ms)).where(
            DailyMetric.user_id == user_id,
            DailyMetric.date >= hist_start,
            DailyMetric.date <= hist_end,
            DailyMetric.hrv_ms.is_not(None),
        )
    )
    if not current_avg or not hist_avg or (current_count or 0) < 10 or (hist_count or 0) < 10:
        return None

    abs_change = round(float(current_avg) - float(hist_avg), 1)
    pct_change = round((abs_change / float(hist_avg)) * 100, 1)
    if abs(pct_change) < 5.0:
        return None  # noise — not worth surfacing

    return {
        "current_30d_avg_ms": round(float(current_avg), 1),
        "historical_30d_avg_ms": round(float(hist_avg), 1),
        "change_ms": abs_change,
        "change_pct": pct_change,
        "direction": "improving" if abs_change > 0 else "declining",
        "comparison_period": "past 90 days",
    }


def _get_bedtime_deviation(session: Session, user_id: int, target_date: date) -> dict | None:
    from app.services.sleep_optimizer import get_bedtime_deviation
    return get_bedtime_deviation(session, user_id, target_date)


def safety_message(triggers: list[str]) -> str | None:
    """Hard-coded caution text appended by the backend, never written by the LLM."""
    if not triggers:
        return None
    described = "; ".join(SAFETY_TRIGGER_DESCRIPTIONS[t] for t in triggers)
    return (
        f"⚠️ One more thing: {described}. This pattern is worth taking seriously. "
        "I can't diagnose anything, but if this continues or you feel unwell, "
        "it would be smart to talk with a medical professional."
    )


def _row_for(session: Session, user_id: int, day: date) -> DailyMetric | None:
    return session.scalar(
        select(DailyMetric).where(DailyMetric.user_id == user_id, DailyMetric.date == day)
    )


def _avg(
    session: Session,
    user_id: int,
    column: InstrumentedAttribute,
    start: date,
    end: date,
) -> float | None:
    value = session.scalar(
        select(func.avg(column)).where(
            DailyMetric.user_id == user_id,
            DailyMetric.date >= start,
            DailyMetric.date <= end,
            column.is_not(None),
        )
    )
    return float(value) if value is not None else None


def _summarize(
    session: Session,
    user_id: int,
    target_date: date,
    column: InstrumentedAttribute,
    today_row: DailyMetric | None,
) -> MetricSummary:
    # baselines exclude today: "today vs YOUR normal" needs normal to not include today
    yesterday = target_date - timedelta(days=1)
    return MetricSummary(
        today=getattr(today_row, column.key) if today_row else None,
        baseline_7d=_avg(session, user_id, column, target_date - timedelta(days=7), yesterday),
        baseline_30d=_avg(session, user_id, column, target_date - timedelta(days=30), yesterday),
        flag=None,
    )


def _flag_low_high(summary: MetricSummary, delta: float, low_flag: str, high_flag: str) -> str | None:
    if summary.today is None or summary.baseline_30d is None:
        return None
    if summary.today < summary.baseline_30d - delta:
        return low_flag
    if summary.today > summary.baseline_30d + delta:
        return high_flag
    return None


def _flag_rhr(summary: MetricSummary) -> str | None:
    if summary.today is None or summary.baseline_30d is None:
        return None
    if summary.today > summary.baseline_30d + RHR_ELEVATED_BPM:
        return "elevated"
    if summary.today < summary.baseline_30d - RHR_ELEVATED_BPM:
        return "lower_than_usual"
    return None


def _flag_hrv(summary: MetricSummary) -> str | None:
    if summary.today is None or summary.baseline_30d is None:
        return None
    if summary.today < summary.baseline_30d * HRV_BELOW_RATIO:
        return "below_baseline"
    if summary.today > summary.baseline_30d * (2 - HRV_BELOW_RATIO):
        return "above_baseline"
    return None


def _classify_strain(strain: float | None) -> str | None:
    if strain is None:
        return None
    if strain >= STRAIN_HIGH:
        return "high"
    if strain >= STRAIN_MODERATE:
        return "moderate"
    return "low"


def _data_days_available(session: Session, user_id: int, target_date: date) -> int:
    value = session.scalar(
        select(func.count(DailyMetric.id)).where(
            DailyMetric.user_id == user_id,
            DailyMetric.date <= target_date,
            (DailyMetric.recovery_score.is_not(None)) | (DailyMetric.sleep_hours.is_not(None)),
        )
    )
    return int(value or 0)


def _maturity(data_days: int) -> str:
    if data_days < 7:
        return "building_baseline"
    if data_days < 30:
        return "early_baseline"
    return "established"


def _build_weight_trend(session: Session, user_id: int, target_date: date) -> WeightTrend | None:
    """Compute overnight weight change and weekly trend from Withings data (kg → lbs)."""
    window_start = target_date - timedelta(days=16)
    rows = session.scalars(
        select(DailyMetric)
        .where(
            DailyMetric.user_id == user_id,
            DailyMetric.date >= window_start,
            DailyMetric.date <= target_date,
            DailyMetric.weight.is_not(None),
        )
        .order_by(DailyMetric.date.desc())
    ).all()

    if not rows:
        return None

    # Overnight change: today vs the most recent previous reading
    today_weight: float | None = None
    prev_weight: float | None = None
    for r in rows:
        if r.date == target_date:
            today_weight = r.weight
        elif today_weight is not None and prev_weight is None:
            prev_weight = r.weight
            break

    overnight_change_lbs: float | None = None
    if today_weight is not None and prev_weight is not None:
        overnight_change_lbs = round((today_weight - prev_weight) * KG_TO_LBS, 1)

    # Weekly trend: avg of recent 7 days vs avg of prior 7 days (days 8-16)
    today_ord = target_date.toordinal()
    week1 = [r.weight for r in rows if (today_ord - r.date.toordinal()) < 7]
    week2 = [r.weight for r in rows if 7 <= (today_ord - r.date.toordinal()) < 16]

    weekly_trend_lbs: float | None = None
    if week1 and week2:
        diff_kg = (sum(week1) / len(week1)) - (sum(week2) / len(week2))
        weekly_trend_lbs = round(diff_kg * KG_TO_LBS, 1)

    flag: str | None = None
    if overnight_change_lbs is not None and abs(overnight_change_lbs) >= WATER_SPIKE_LBS:
        flag = "spike_likely_water"
    elif weekly_trend_lbs is not None:
        if weekly_trend_lbs <= -WEIGHT_TREND_FLAG_LBS:
            flag = "declining"
        elif weekly_trend_lbs >= WEIGHT_TREND_FLAG_LBS:
            flag = "gaining"

    if overnight_change_lbs is None and weekly_trend_lbs is None:
        return None

    current_weight_lbs: float | None = None
    if rows:
        latest = next((r for r in rows if r.weight is not None), None)
        if latest:
            current_weight_lbs = round(latest.weight * KG_TO_LBS, 1)

    projected_2w_lbs: float | None = None
    if current_weight_lbs is not None and weekly_trend_lbs is not None:
        projected_2w_lbs = round(current_weight_lbs + (weekly_trend_lbs * 2), 1)

    return WeightTrend(
        overnight_change_lbs=overnight_change_lbs,
        weekly_trend_lbs=weekly_trend_lbs,
        flag=flag,
        current_weight_lbs=current_weight_lbs,
        projected_2w_lbs=projected_2w_lbs,
    )


def _safety_triggers(
    session: Session, user_id: int, target_date: date, rhr_baseline_30d: float | None,
    weight_trend: WeightTrend | None = None,
) -> list[str]:
    window_start = target_date - timedelta(days=max(RHR_STREAK_DAYS, RECOVERY_STREAK_DAYS) + 2)
    rows = session.scalars(
        select(DailyMetric)
        .where(
            DailyMetric.user_id == user_id,
            DailyMetric.date >= window_start,
            DailyMetric.date <= target_date,
        )
        .order_by(DailyMetric.date.desc())
    ).all()
    by_date = {row.date: row for row in rows}

    triggers: list[str] = []
    if rhr_baseline_30d is not None:
        threshold = rhr_baseline_30d + RHR_ELEVATED_BPM
        if _consecutive_days(by_date, target_date, lambda r: r.resting_heart_rate is not None and r.resting_heart_rate > threshold) >= RHR_STREAK_DAYS:
            triggers.append("rhr_elevated_5d")
    if _consecutive_days(by_date, target_date, lambda r: r.recovery_score is not None and r.recovery_score < RECOVERY_VERY_LOW) >= RECOVERY_STREAK_DAYS:
        triggers.append("recovery_low_7d")
    if (
        weight_trend is not None
        and weight_trend.weekly_trend_lbs is not None
        and weight_trend.weekly_trend_lbs <= -WEIGHT_LOSS_SAFETY_LBS
    ):
        triggers.append("weight_loss_rapid")
    return triggers


def _consecutive_days(by_date: dict, target_date: date, predicate) -> int:
    streak = 0
    day = target_date
    while True:
        row = by_date.get(day)
        if row is None or not predicate(row):
            return streak
        streak += 1
        day -= timedelta(days=1)


def _get_tag_streaks(session: Session, user_id: int, target_date: date, min_streak: int = 2) -> list[dict]:
    """Return tags logged on consecutive days ending yesterday, longest streak first."""
    from app.models.journal_entry import JournalEntry

    yesterday = target_date - timedelta(days=1)
    cutoff = target_date - timedelta(days=10)
    entries = session.scalars(
        select(JournalEntry).where(
            JournalEntry.user_id == user_id,
            JournalEntry.date >= cutoff,
            JournalEntry.date <= yesterday,
        )
    ).all()
    by_date: dict[date, set[str]] = {e.date: set(e.tags or []) for e in entries}

    if yesterday not in by_date:
        return []

    streaks = []
    for tag in by_date[yesterday]:
        count = 0
        day = yesterday
        while day in by_date and tag in by_date[day]:
            count += 1
            day -= timedelta(days=1)
        if count >= min_streak:
            streaks.append({"tag": tag, "days": count})

    streaks.sort(key=lambda x: x["days"], reverse=True)
    return streaks


def _get_supplement_streak(session: Session, user_id: int, target_date: date, name: str = "creatine") -> int:
    """Count consecutive days a supplement was taken, up to and including yesterday."""
    from app.models.supplement_log import SupplementLog

    yesterday = target_date - timedelta(days=1)
    cutoff = target_date - timedelta(days=60)
    rows = session.scalars(
        select(SupplementLog).where(
            SupplementLog.user_id == user_id,
            SupplementLog.name == name,
            SupplementLog.date >= cutoff,
            SupplementLog.date <= yesterday,
        )
    ).all()
    by_date: dict[date, bool] = {r.date: r.taken for r in rows}
    streak = 0
    day = yesterday
    while by_date.get(day):
        streak += 1
        day -= timedelta(days=1)
    return streak


_INTENTIONAL_LOSS_KEYWORDS = frozenset({
    "peptide", "semaglutide", "ozempic", "wegovy", "mounjaro", "tirzepatide",
    "glp", "cut", "cutting", "intentional", "deficit", "dieting",
})


def _is_intentional_weight_loss(coach_notes: dict) -> bool:
    """True if coach_notes suggests the user is intentionally losing weight."""
    for val in coach_notes.values():
        if isinstance(val, list):
            for item in val:
                if any(kw in str(item).lower() for kw in _INTENTIONAL_LOSS_KEYWORDS):
                    return True
        elif any(kw in str(val).lower() for kw in _INTENTIONAL_LOSS_KEYWORDS):
            return True
    return False


def get_previous_daily_message(session: Session, user_id: int, before_date: date) -> str | None:
    """The text of the last daily coach message strictly before `before_date`.

    Lets the morning message build on what it said yesterday instead of cold-opening.
    Excludes today's own row, so a second /today in the same morning still references
    yesterday, not the message we just generated.
    """
    from app.models.coach_message import CoachMessage

    return session.scalar(
        select(CoachMessage.ai_response)
        .where(
            CoachMessage.user_id == user_id,
            CoachMessage.message_type == "daily",
            CoachMessage.date < before_date,
            CoachMessage.ai_response != "",
        )
        .order_by(CoachMessage.date.desc(), CoachMessage.id.desc())
        .limit(1)
    )


def should_gap_fill(session: Session, user_id: int, target_date: date, snapshot: DailySnapshot) -> bool:
    """True when recovery is notably low but nothing was logged about yesterday.

    The backend decides whether to ask; the AI only phrases the question.
    Gates: recovery is flagged low vs baseline OR outright very low (<50), AND
    the user has no journal entry and no events for yesterday.
    """
    rec = snapshot.recovery
    low = rec.flag == "low_vs_baseline" or (rec.today is not None and rec.today < 50)
    if not low:
        return False

    from app.models.journal_entry import JournalEntry
    from app.models.event import Event

    yesterday = target_date - timedelta(days=1)
    has_journal = bool(session.scalar(
        select(func.count(JournalEntry.id)).where(
            JournalEntry.user_id == user_id, JournalEntry.date == yesterday
        )
    ))
    if has_journal:
        return False

    has_events = bool(session.scalar(
        select(func.count(Event.id)).where(
            Event.user_id == user_id, Event.local_date == yesterday
        )
    ))
    return not has_events


def get_checkin_streak(session: Session, user_id: int, target_date: date) -> int:
    """Count consecutive days with a journal entry, looking back from yesterday."""
    from app.models.journal_entry import JournalEntry

    lookback = target_date - timedelta(days=60)
    journal_dates = set(
        session.scalars(
            select(JournalEntry.date).where(
                JournalEntry.user_id == user_id,
                JournalEntry.date >= lookback,
                JournalEntry.date < target_date,
            )
        ).all()
    )

    streak = 0
    day = target_date - timedelta(days=1)
    while day >= lookback:
        if day in journal_dates:
            streak += 1
            day -= timedelta(days=1)
        else:
            break
    return streak


# ---------------------------------------------------------------------------
# Weekly snapshot
# ---------------------------------------------------------------------------

@dataclass
class WeeklySnapshot:
    recovery: MetricSummary
    sleep_hours: MetricSummary
    resting_hr: MetricSummary
    hrv: MetricSummary
    avg_strain_7d: float | None
    data_days_available: int
    data_maturity: str
    tag_patterns: list[dict] | None = None  # top behavior-recovery correlations (30d)


def build_weekly_snapshot(session: Session, user_id: int, target_date: date) -> WeeklySnapshot:
    yesterday = target_date - timedelta(days=1)
    week_start = target_date - timedelta(days=7)
    month_start = target_date - timedelta(days=30)

    def _weekly(col: InstrumentedAttribute, higher_is_better: bool = True) -> MetricSummary:
        week_avg = _avg(session, user_id, col, week_start, yesterday)
        month_avg = _avg(session, user_id, col, month_start, yesterday)
        flag = None
        if week_avg is not None and month_avg is not None and month_avg != 0:
            ratio = week_avg / month_avg
            if higher_is_better:
                flag = "above_baseline" if ratio > 1.1 else ("below_baseline" if ratio < 0.9 else None)
            else:
                flag = "elevated" if ratio > 1.1 else ("lower_than_usual" if ratio < 0.9 else None)
        return MetricSummary(today=week_avg, baseline_7d=None, baseline_30d=month_avg, flag=flag)

    from app.services.sleep_optimizer import get_pre_sleep_factor_impact
    raw_patterns = get_pre_sleep_factor_impact(session, user_id, lookback_days=30)
    # Keep top 5 by absolute delta — most striking patterns, both good and bad
    tag_patterns = sorted(raw_patterns, key=lambda x: abs(x.get("delta_vs_no_tag") or 0), reverse=True)[:5]

    return WeeklySnapshot(
        recovery=_weekly(DailyMetric.recovery_score),
        sleep_hours=_weekly(DailyMetric.sleep_hours),
        resting_hr=_weekly(DailyMetric.resting_heart_rate, higher_is_better=False),
        hrv=_weekly(DailyMetric.hrv_ms),
        avg_strain_7d=_avg(session, user_id, DailyMetric.strain, week_start, yesterday),
        data_days_available=_data_days_available(session, user_id, target_date),
        data_maturity=_maturity(_data_days_available(session, user_id, target_date)),
        tag_patterns=tag_patterns or None,
    )


# ---------------------------------------------------------------------------
# Q&A context
# ---------------------------------------------------------------------------

@dataclass
class QAContext:
    data_days_available: int
    data_maturity: str
    avg_7d: dict[str, float | None]
    avg_30d: dict[str, float | None]
    recent_tags: list[str]
    observations: list[str]
    recent_daily_data: list[dict]  # per-day actuals, newest-first, last 7 days
    today_date: str  # "YYYY-MM-DD" so AI knows which row is today
    user_name: str | None = None
    max_heart_rate: float | None = None
    height_meter: float | None = None
    user_goal: str | None = None
    recent_events: list[dict] | None = None  # last 14 days of logged events
    supplement_history: list[dict] | None = None  # last 30 days of supplement logs
    coach_notes: dict | None = None  # persistent facts the coach has learned
    sleep_insights: dict | None = None  # bedtime profile, optimal window, factor impact
    goal_weight_lbs: float | None = None  # user's target weight in lbs
    hrv_long_trend: dict | None = None    # 30d HRV avg now vs 60-90d ago
    workout_effect: dict | None = None   # recovery the day after workout vs rest
    weight_velocity: dict | None = None  # weight change rate now vs prior 4 weeks
    weight_projection: dict | None = None  # deterministic projection to goal weight
    weight_current_lbs: float | None = None      # raw current weight (for question-aware projection)
    weight_weekly_rate_lbs: float | None = None  # raw signed weekly trend (negative = losing)
    weight_trend_audit: dict | None = None       # per-window trends + selected rate + known rows


def build_qa_context(session: Session, user_id: int, target_date: date, user=None) -> QAContext:
    from app.models.event import Event
    from app.models.journal_entry import JournalEntry
    from app.models.observation import Observation
    from app.models.supplement_log import SupplementLog
    from app.services.sleep_optimizer import build_sleep_insights

    yesterday = target_date - timedelta(days=1)
    week_start = target_date - timedelta(days=7)
    month_start = target_date - timedelta(days=30)

    kg_to_lbs = lambda v: round(v * 2.20462, 1) if v is not None else None  # noqa: E731
    metric_cols = {
        "recovery": DailyMetric.recovery_score,
        "sleep_hours": DailyMetric.sleep_hours,
        "hrv_ms": DailyMetric.hrv_ms,
        "resting_hr": DailyMetric.resting_heart_rate,
        "strain": DailyMetric.strain,
        "rem_sleep_hours": DailyMetric.rem_sleep_hours,
        "deep_sleep_hours": DailyMetric.deep_sleep_hours,
        "body_fat_pct": DailyMetric.body_fat_pct,
    }
    mass_cols = {
        "weight_lbs": DailyMetric.weight,
        "muscle_mass_lbs": DailyMetric.muscle_mass,
    }
    avg_7d = {k: _avg(session, user_id, col, week_start, yesterday) for k, col in metric_cols.items()}
    avg_30d = {k: _avg(session, user_id, col, month_start, yesterday) for k, col in metric_cols.items()}
    for k, col in mass_cols.items():
        avg_7d[k] = kg_to_lbs(_avg(session, user_id, col, week_start, yesterday))
        avg_30d[k] = kg_to_lbs(_avg(session, user_id, col, month_start, yesterday))

    recent_entries = session.scalars(
        select(JournalEntry).where(
            JournalEntry.user_id == user_id,
            JournalEntry.date >= week_start,
        )
    ).all()
    recent_tags = list({tag for entry in recent_entries for tag in (entry.tags or [])})

    _KG_TO_LBS = 2.20462
    recent_rows = session.scalars(
        select(DailyMetric)
        .where(DailyMetric.user_id == user_id, DailyMetric.date <= target_date)
        .order_by(DailyMetric.date.desc())
        .limit(7)
    ).all()
    recent_daily_data = []
    for r in recent_rows:
        day: dict = {"date": str(r.date)}
        if r.recovery_score is not None:
            day["recovery"] = round(r.recovery_score, 1)
        if r.sleep_hours is not None:
            day["sleep_hours"] = round(r.sleep_hours, 1)
        if r.resting_heart_rate is not None:
            day["resting_hr"] = round(r.resting_heart_rate, 1)
        if r.hrv_ms is not None:
            day["hrv_ms"] = round(r.hrv_ms, 1)
        if r.strain is not None:
            day["strain"] = round(r.strain, 1)
        if r.sleep_start_local:
            day["bedtime"] = r.sleep_start_local
        if r.sleep_end_local:
            day["wake_time"] = r.sleep_end_local
        if r.rem_sleep_hours is not None:
            day["rem_hours"] = round(r.rem_sleep_hours, 1)
        if r.deep_sleep_hours is not None:
            day["deep_hours"] = round(r.deep_sleep_hours, 1)
        if r.workout_count:
            day["workout_count"] = r.workout_count
        if r.total_workout_minutes is not None:
            day["workout_minutes"] = round(r.total_workout_minutes, 0)
        if r.weight is not None:
            day["weight_lbs"] = round(r.weight * _KG_TO_LBS, 1)
        if r.body_fat_pct is not None:
            day["body_fat_pct"] = round(r.body_fat_pct, 1)
        if r.muscle_mass is not None:
            day["muscle_mass_lbs"] = round(r.muscle_mass * _KG_TO_LBS, 1)
        recent_daily_data.append(day)

    obs_rows = session.scalars(
        select(Observation)
        .where(Observation.user_id == user_id, Observation.status != "archived")
        .order_by(Observation.occurrence_count.desc())
        .limit(10)
    ).all()
    observations = [
        f"{o.pattern_description} Evidence: {o.supporting_count} of {o.occurrence_count} logged days."
        for o in obs_rows
    ]

    events_cutoff = target_date - timedelta(days=14)
    event_rows = session.scalars(
        select(Event)
        .where(
            Event.user_id == user_id,
            Event.local_date >= events_cutoff,
            Event.local_date <= target_date,
        )
        .order_by(Event.local_date.desc(), Event.occurred_at.desc())
    ).all()
    recent_events = [
        {
            "date": str(r.local_date),
            "type": r.event_type,
            "text": r.raw_text,
        }
        for r in event_rows
    ]

    supp_cutoff = target_date - timedelta(days=30)
    supp_rows = session.scalars(
        select(SupplementLog)
        .where(
            SupplementLog.user_id == user_id,
            SupplementLog.date >= supp_cutoff,
            SupplementLog.date <= target_date,
        )
        .order_by(SupplementLog.date.desc())
    ).all()
    supplement_history = [
        {
            "date": str(r.date),
            "name": r.name,
            "taken": r.taken,
        }
        for r in supp_rows
    ] or None

    raw_notes = getattr(user, "coach_notes", None) if user else None
    coach_notes = raw_notes if isinstance(raw_notes, dict) and raw_notes else None

    # Deterministic weight-trend audit + projection (backend computes; AI narrates).
    # The audit exposes per-window trends from the ACTUAL dated readings and the
    # selected rate (window + method), so the AI can't reconstruct or mis-date
    # weights. Raw current weight + selected signed rate feed question-aware
    # projection in the payload builder.
    weight_projection = None
    weight_current_lbs = None
    weight_weekly_rate_lbs = None
    weight_trend_audit = None
    goal_weight = getattr(user, "goal_weight_lbs", None) if user else None
    from app.services import weight_trends
    from app.services.weight_projection import project_weight
    _wt_cutoff = target_date - timedelta(days=35)
    _wt_rows = session.scalars(
        select(DailyMetric).where(
            DailyMetric.user_id == user_id,
            DailyMetric.date >= _wt_cutoff,
            DailyMetric.date <= target_date,
            DailyMetric.weight.is_not(None),
        )
    ).all()
    _weights_lbs = [(r.date, r.weight * 2.20462) for r in _wt_rows]
    weight_trend_audit = weight_trends.build_weight_trend_audit(_weights_lbs, target_date)
    if weight_trend_audit is not None:
        weight_current_lbs = weight_trend_audit["current_weight"]
        _sel = weight_trend_audit.get("selected")
        weight_weekly_rate_lbs = _sel["rate_lbs_per_week"] if _sel else None
        if goal_weight is not None:
            weight_projection = project_weight(
                weight_current_lbs, goal_weight, weight_weekly_rate_lbs, target_date,
                rate_window_days=(_sel or {}).get("window_days"),
                rate_method=(_sel or {}).get("method"),
            )

    data_days = _data_days_available(session, user_id, target_date)
    return QAContext(
        data_days_available=data_days,
        data_maturity=_maturity(data_days),
        avg_7d=avg_7d,
        avg_30d=avg_30d,
        recent_tags=recent_tags,
        observations=observations,
        recent_daily_data=recent_daily_data,
        today_date=str(target_date),
        user_name=getattr(user, "first_name", None) if user else None,
        max_heart_rate=getattr(user, "max_heart_rate", None) if user else None,
        height_meter=getattr(user, "height_meter", None) if user else None,
        user_goal=getattr(user, "goal", None) if user else None,
        recent_events=recent_events or None,
        supplement_history=supplement_history,
        coach_notes=coach_notes,
        sleep_insights=build_sleep_insights(session, user_id),
        goal_weight_lbs=goal_weight,
        hrv_long_trend=get_hrv_baseline_trend(session, user_id, target_date),
        workout_effect=get_workout_recovery_effect(session, user_id, target_date),
        weight_velocity=get_weight_velocity(session, user_id, target_date),
        weight_projection=weight_projection,
        weight_current_lbs=weight_current_lbs,
        weight_weekly_rate_lbs=weight_weekly_rate_lbs,
        weight_trend_audit=weight_trend_audit,
    )


_SAFETY_WARNING_COOLDOWN_DAYS = 3


def filter_fresh_triggers(
    session: Session,
    user_id: int,
    target_date: date,
    triggers: list[str],
    coach_notes: dict | None = None,
) -> list[str]:
    """Return only triggers not already shown in recent daily messages.

    Also permanently suppresses weight_loss_rapid when coach_notes indicates
    the user is intentionally losing weight (peptides, cutting, etc.) — they
    already know why and the warning adds no value.
    """
    if not triggers:
        return []
    from app.models.coach_message import CoachMessage

    # Suppress intentional-loss warning permanently
    intentional = coach_notes and _is_intentional_weight_loss(coach_notes)

    cutoff = target_date - timedelta(days=_SAFETY_WARNING_COOLDOWN_DAYS)
    recent = session.scalars(
        select(CoachMessage).where(
            CoachMessage.user_id == user_id,
            CoachMessage.message_type == "daily",
            CoachMessage.date >= cutoff,
            CoachMessage.date < target_date,
        )
    ).all()

    already_shown: set[str] = set()
    for msg in recent:
        if not msg.ai_response:
            continue
        for key, desc in SAFETY_TRIGGER_DESCRIPTIONS.items():
            if desc[:30] in msg.ai_response:
                already_shown.add(key)

    result = []
    for t in triggers:
        if t in already_shown:
            continue
        if t == "weight_loss_rapid" and intentional:
            continue
        result.append(t)
    return result
