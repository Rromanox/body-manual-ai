"""Recovery gate — extends the morning check-in with a training recommendation.

Reads today's WHOOP recovery %, plus HRV and RHR against the user's own 14-day
rolling baseline, and returns a recommendation for today's session. It NEVER
mutates the session's type/duration itself: it writes ``recovery_adjustment`` and
the morning message offers Accept / Ride-as-written buttons. Accept -> status
modified; override -> logged, session unchanged. Missing WHOOP data is handled
explicitly (late sync) — the session shows as written and yesterday's score is
never reused.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.daily_metric import DailyMetric
from app.models.training_session import TrainingSession
from app.services import training_plan as tp

logger = logging.getLogger(__name__)

GREEN_MIN = 67          # recovery >= 67 -> green
YELLOW_MIN = 34         # 34..66 -> yellow ; < 34 -> red
HRV_LOW_RATIO = 0.85    # > 15% below baseline
HRV_LOW_DAYS = 2        # consecutive days for the red trigger
RHR_ALARM_BPM = 5.0     # >= baseline + 5
RHR_ALARM_DAYS = 3      # consecutive days for the alarm
BASELINE_DAYS = 14
FUELING_LOSS_LBS = 1.0  # losing more than this per week on a fueling Sunday
KG_TO_LBS = 2.20462


@dataclass
class GateResult:
    zone: str                       # green | yellow | red | no_data
    recovery: float | None
    session_type: str | None
    adjustment: str | None = None   # recovery_adjustment text (None = as written)
    adjustment_offered: bool = False
    rhr_alarm: bool = False
    hrv_2day_low: bool = False
    hrv_baseline: float | None = None
    rhr_baseline: float | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def recovery_line(self) -> str:
        """The 'Recovery: 71% (green) -> as written' line of the check-in block."""
        if self.zone == "no_data":
            return "Recovery: no data yet → as written"
        pct = f"{round(self.recovery)}%"
        tail = self.adjustment if self.adjustment else "as written"
        return f"Recovery: {pct} ({self.zone}) → {tail}"


def _row(session: Session, user_id: int, d: date) -> DailyMetric | None:
    return session.scalar(
        select(DailyMetric).where(DailyMetric.user_id == user_id, DailyMetric.date == d)
    )


def _baseline(session: Session, user_id: int, column, d: date) -> float | None:
    """14-day rolling average of ``column`` ending yesterday (excludes today)."""
    value = session.scalar(
        select(func.avg(column)).where(
            DailyMetric.user_id == user_id,
            DailyMetric.date >= d - timedelta(days=BASELINE_DAYS),
            DailyMetric.date <= d - timedelta(days=1),
            column.is_not(None),
        )
    )
    return float(value) if value is not None else None


def _consecutive(session: Session, user_id: int, d: date, predicate) -> int:
    """Consecutive days ending on ``d`` where ``predicate(row)`` holds."""
    rows = {
        r.date: r
        for r in session.scalars(
            select(DailyMetric).where(
                DailyMetric.user_id == user_id,
                DailyMetric.date >= d - timedelta(days=RHR_ALARM_DAYS + 2),
                DailyMetric.date <= d,
            )
        ).all()
    }
    streak = 0
    day = d
    while True:
        row = rows.get(day)
        if row is None or not predicate(row):
            return streak
        streak += 1
        day -= timedelta(days=1)


def _recommend(row: TrainingSession | None, zone: str) -> str | None:
    """The adjustment text for today's session at a given zone, or None = as written.

    Saturday long rides are the deliberate exception: they run as written on
    yellow (fatigue tolerance is the goal) and are only reduced on red.
    """
    if row is None or row.session_type == "rest":
        return None
    st = row.session_type
    is_saturday = row.date.weekday() == 5

    if zone == "red":
        if st == "z2":
            return "Make it an easy 45 min Z2, or take the day off"
        return "Swap today for 45 min easy Z2 or rest"
    if zone == "yellow":
        if st == "long_ride" and is_saturday:
            return None  # Saturday long-ride exception
        if st == "intervals":
            return "Reduce to tempo — swap the sweet-spot blocks for steady tempo (or drop one interval)"
        if st == "tempo":
            return "Drop to steady Z2 today"
        if st in ("gym_a", "gym_b"):
            return "Drop one set from every exercise"
        return None  # z2 / other already easy
    return None  # green


def evaluate_gate(session: Session, user_id: int, d: date) -> GateResult:
    today = _row(session, user_id, d)
    training = tp.get_session(session, user_id, d)
    session_type = training.session_type if training else None

    recovery = today.recovery_score if today else None

    hrv_baseline = _baseline(session, user_id, DailyMetric.hrv_ms, d)
    rhr_baseline = _baseline(session, user_id, DailyMetric.resting_heart_rate, d)

    hrv_2day_low = False
    if hrv_baseline:
        threshold = hrv_baseline * HRV_LOW_RATIO
        hrv_2day_low = _consecutive(
            session, user_id, d,
            lambda r: r.hrv_ms is not None and r.hrv_ms < threshold,
        ) >= HRV_LOW_DAYS

    rhr_alarm = False
    if rhr_baseline:
        threshold = rhr_baseline + RHR_ALARM_BPM
        rhr_alarm = _consecutive(
            session, user_id, d,
            lambda r: r.resting_heart_rate is not None and r.resting_heart_rate > threshold,
        ) >= RHR_ALARM_DAYS

    # Zone. Missing recovery is its own state — never reuse yesterday's score.
    if recovery is None:
        zone = "no_data"
    elif recovery < YELLOW_MIN or hrv_2day_low:
        zone = "red"
    elif recovery <= GREEN_MIN - 1:
        zone = "yellow"
    else:
        zone = "green"

    adjustment = _recommend(training, zone) if zone != "no_data" else None

    notes: list[str] = []
    # RHR alarm is prominent and fires regardless of today's score.
    if rhr_alarm:
        notes.append(
            "⚠️ Your resting heart rate has been 5+ bpm above your baseline for 3+ days — "
            "consider an extra rest day."
        )
        if adjustment is None:
            adjustment = "Take an extra rest day — resting HR has been elevated for 3+ days"
    if hrv_2day_low:
        notes.append("HRV has been 15%+ below your baseline for 2+ days.")

    return GateResult(
        zone=zone,
        recovery=recovery,
        session_type=session_type,
        adjustment=adjustment,
        adjustment_offered=adjustment is not None,
        rhr_alarm=rhr_alarm,
        hrv_2day_low=hrv_2day_low,
        hrv_baseline=hrv_baseline,
        rhr_baseline=rhr_baseline,
        notes=notes,
    )


# --- persistence: recommendation + user's response --------------------------

def record_recommendation(
    session: Session, user_id: int, d: date, gate: GateResult, *, source: str = "system", commit: bool = True
) -> None:
    """Persist the gate's recommendation onto the session row (recovery_adjustment)
    and log it. Called by the morning check-in when an adjustment is offered."""
    row = tp.get_session(session, user_id, d)
    if row is not None and gate.adjustment:
        row.recovery_adjustment = gate.adjustment
    tp.log_action(
        session, user_id, action="gate_recommendation", source=source, session_date=d,
        detail={
            "zone": gate.zone, "recovery": gate.recovery,
            "adjustment": gate.adjustment, "rhr_alarm": gate.rhr_alarm,
        },
        commit=commit,
    )


def accept_adjustment(
    session: Session, user_id: int, d: date, *, source: str = "command", commit: bool = True
) -> TrainingSession | None:
    """User accepted the gate's adjustment: status -> modified, adjustment retained.
    The session's planned type/duration are intentionally NOT mutated."""
    row = tp.get_session(session, user_id, d)
    if row is None:
        return None
    row.status = "modified"
    tp.log_action(
        session, user_id, action="gate_accepted", source=source, session_date=d,
        detail={"adjustment": row.recovery_adjustment}, commit=commit,
    )
    return row


def override_adjustment(
    session: Session, user_id: int, d: date, *, source: str = "command", commit: bool = True
) -> TrainingSession | None:
    """User chose to ride as written: session unchanged, choice logged."""
    row = tp.get_session(session, user_id, d)
    if row is None:
        return None
    tp.log_action(
        session, user_id, action="gate_overridden", source=source, session_date=d,
        detail={"adjustment": row.recovery_adjustment}, commit=commit,
    )
    return row


# --- fueling check (Sundays, weeks 5-10) ------------------------------------

def fueling_flag(session: Session, user_id: int, d: date) -> dict | None:
    """On weeks 5-10 Sundays, flag under-fueling if the 7-day weight trend is
    losing more than 1 lb/week. Highest-priority flag for the weekly summary."""
    if d.weekday() != 6:  # Sunday
        return None
    week = tp.week_of(d)
    if week is None or not (5 <= week <= 10):
        return None

    rows = session.scalars(
        select(DailyMetric)
        .where(
            DailyMetric.user_id == user_id,
            DailyMetric.date >= d - timedelta(days=13),
            DailyMetric.date <= d,
            DailyMetric.weight.is_not(None),
        )
        .order_by(DailyMetric.date)
    ).all()
    if len(rows) < 4:
        return None
    ord_today = d.toordinal()
    week1 = [r.weight for r in rows if (ord_today - r.date.toordinal()) < 7]
    week2 = [r.weight for r in rows if 7 <= (ord_today - r.date.toordinal()) < 14]
    if not week1 or not week2:
        return None
    weekly_lbs = round(((sum(week1) / len(week1)) - (sum(week2) / len(week2))) * KG_TO_LBS, 1)
    if weekly_lbs < -FUELING_LOSS_LBS:
        return {"flag": "under_fueling", "weekly_lbs": weekly_lbs, "week": week}
    return None
