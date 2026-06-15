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

SAFETY_TRIGGER_DESCRIPTIONS = {
    "rhr_elevated_5d": "your resting heart rate has been above your normal for 5+ days in a row",
    "recovery_low_7d": "your recovery has been very low for 7+ days in a row",
}


@dataclass
class MetricSummary:
    today: float | None
    baseline_7d: float | None
    baseline_30d: float | None
    flag: str | None


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

    return DailySnapshot(
        target_date=target_date,
        recovery=recovery,
        sleep_hours=sleep_hours,
        resting_hr=resting_hr,
        hrv=hrv,
        yesterday_strain=_classify_strain(yesterday_row.strain if yesterday_row else None),
        yesterday_workout_count=yesterday_row.workout_count if yesterday_row else None,
        yesterday_workout_minutes=yesterday_row.total_workout_minutes if yesterday_row else None,
        data_days_available=data_days,
        data_maturity=_maturity(data_days),
        safety_triggers=_safety_triggers(session, user_id, target_date, resting_hr.baseline_30d),
    )


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


def _safety_triggers(
    session: Session, user_id: int, target_date: date, rhr_baseline_30d: float | None
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

    return WeeklySnapshot(
        recovery=_weekly(DailyMetric.recovery_score),
        sleep_hours=_weekly(DailyMetric.sleep_hours),
        resting_hr=_weekly(DailyMetric.resting_heart_rate, higher_is_better=False),
        hrv=_weekly(DailyMetric.hrv_ms),
        avg_strain_7d=_avg(session, user_id, DailyMetric.strain, week_start, yesterday),
        data_days_available=_data_days_available(session, user_id, target_date),
        data_maturity=_maturity(_data_days_available(session, user_id, target_date)),
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
    max_heart_rate: float | None = None
    height_meter: float | None = None


def build_qa_context(session: Session, user_id: int, target_date: date, user=None) -> QAContext:
    from app.models.journal_entry import JournalEntry
    from app.models.observation import Observation

    yesterday = target_date - timedelta(days=1)
    week_start = target_date - timedelta(days=7)
    month_start = target_date - timedelta(days=30)

    metric_cols = {
        "recovery": DailyMetric.recovery_score,
        "sleep_hours": DailyMetric.sleep_hours,
        "hrv_ms": DailyMetric.hrv_ms,
        "resting_hr": DailyMetric.resting_heart_rate,
        "strain": DailyMetric.strain,
        "rem_sleep_hours": DailyMetric.rem_sleep_hours,
        "deep_sleep_hours": DailyMetric.deep_sleep_hours,
        "weight_kg": DailyMetric.weight,
        "body_fat_pct": DailyMetric.body_fat_pct,
        "muscle_mass_kg": DailyMetric.muscle_mass,
    }
    avg_7d = {k: _avg(session, user_id, col, week_start, yesterday) for k, col in metric_cols.items()}
    avg_30d = {k: _avg(session, user_id, col, month_start, yesterday) for k, col in metric_cols.items()}

    recent_entries = session.scalars(
        select(JournalEntry).where(
            JournalEntry.user_id == user_id,
            JournalEntry.date >= week_start,
        )
    ).all()
    recent_tags = list({tag for entry in recent_entries for tag in (entry.tags or [])})

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

    data_days = _data_days_available(session, user_id, target_date)
    return QAContext(
        data_days_available=data_days,
        data_maturity=_maturity(data_days),
        avg_7d=avg_7d,
        avg_30d=avg_30d,
        recent_tags=recent_tags,
        observations=observations,
        max_heart_rate=getattr(user, "max_heart_rate", None) if user else None,
        height_meter=getattr(user, "height_meter", None) if user else None,
    )
