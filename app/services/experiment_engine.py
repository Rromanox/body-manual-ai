"""Experiment engine: user-directed hypothesis testing.

The user names an experiment ("Cutting alcohol", "Earlier bedtime") and the
engine tracks whether relevant metrics move compared to the 14-day baseline
before the start date. Unlike the observation engine (which finds correlations
automatically), experiments test what the user explicitly wants to measure.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.daily_metric import DailyMetric
from app.models.experiment import Experiment

KG_TO_LBS = 2.20462

# Column name → display label and direction (True = higher is better)
METRIC_META: dict[str, tuple[str, bool]] = {
    "recovery_score":     ("recovery",        True),
    "hrv_ms":             ("HRV",             True),
    "sleep_hours":        ("sleep",           True),
    "resting_heart_rate": ("resting HR",      False),
    "weight":             ("weight",          False),
    "strain":             ("strain",          True),
    "sleep_efficiency":   ("sleep eff.",      True),
    "body_fat_pct":       ("body fat",        False),
}

# Minimum delta to call a change "meaningful"
MEANINGFUL_DELTA: dict[str, float] = {
    "recovery_score":     5.0,
    "hrv_ms":             5.0,
    "sleep_hours":        0.4,
    "resting_heart_rate": 3.0,
    "weight":             0.5,   # kg
    "strain":             1.0,
    "sleep_efficiency":   3.0,
    "body_fat_pct":       0.5,
}

# Keyword matching → which metrics to track automatically
_KEYWORD_METRICS: list[tuple[list[str], list[str]]] = [
    (["alcohol", "drink", "wine", "beer", "sober"],
     ["recovery_score", "hrv_ms", "sleep_hours"]),
    (["sleep", "bed", "bedtime", "night"],
     ["recovery_score", "sleep_hours", "hrv_ms"]),
    (["caffeine", "coffee", "tea", "espresso"],
     ["recovery_score", "sleep_hours", "hrv_ms"]),
    (["fast", "fasting", "intermittent", "eating", "meal", "diet", "food", "if"],
     ["recovery_score", "weight", "sleep_hours"]),
    (["workout", "exercise", "training", "gym", "run", "lift", "cardio"],
     ["recovery_score", "hrv_ms", "strain"]),
    (["stress", "meditat", "breath", "relax", "mindful", "journal"],
     ["recovery_score", "hrv_ms", "resting_heart_rate"]),
    (["hydrat", "water"],
     ["recovery_score", "hrv_ms", "resting_heart_rate"]),
    (["weight", "fat", "body comp", "lean"],
     ["weight", "body_fat_pct"]),
]

DEFAULT_METRICS = ["recovery_score", "hrv_ms", "sleep_hours"]
BASELINE_DAYS = 14


def infer_metrics(name: str) -> list[str]:
    lower = name.lower()
    for keywords, metrics in _KEYWORD_METRICS:
        if any(kw in lower for kw in keywords):
            return metrics
    return DEFAULT_METRICS


def start_experiment(
    session: Session,
    user_id: int,
    name: str,
    start_date: date,
    metrics: list[str] | None = None,
) -> Experiment:
    """Create and persist a new experiment, computing baseline from the 14 days before start."""
    if metrics is None:
        metrics = infer_metrics(name)

    baseline_start = start_date - timedelta(days=BASELINE_DAYS)
    baseline_end = start_date - timedelta(days=1)
    baseline_values: dict[str, float | None] = {}
    for metric in metrics:
        col = getattr(DailyMetric, metric, None)
        if col is None:
            continue
        val = session.scalar(
            select(func.avg(col)).where(
                DailyMetric.user_id == user_id,
                DailyMetric.date >= baseline_start,
                DailyMetric.date <= baseline_end,
                col.is_not(None),
            )
        )
        baseline_values[metric] = float(val) if val is not None else None

    exp = Experiment(
        user_id=user_id,
        name=name,
        metrics_to_track=metrics,
        start_date=start_date,
        status="active",
        baseline_values=baseline_values,
    )
    session.add(exp)
    session.commit()
    return exp


def end_experiment(session: Session, experiment: Experiment, end_date: date) -> None:
    experiment.end_date = end_date
    experiment.status = "completed"
    session.commit()


def compute_results(
    session: Session,
    user_id: int,
    experiment: Experiment,
    as_of: date,
) -> dict[str, Any]:
    """Compute current during-experiment averages and deltas vs baseline."""
    end = experiment.end_date or as_of
    days_in = max((end - experiment.start_date).days + 1, 0)

    metric_results: list[dict[str, Any]] = []
    for metric in (experiment.metrics_to_track or []):
        col = getattr(DailyMetric, metric, None)
        if col is None:
            continue
        baseline_val = (experiment.baseline_values or {}).get(metric)
        during_raw = session.scalar(
            select(func.avg(col)).where(
                DailyMetric.user_id == user_id,
                DailyMetric.date >= experiment.start_date,
                DailyMetric.date <= end,
                col.is_not(None),
            )
        )
        if during_raw is None:
            continue
        during_val = float(during_raw)
        delta = (during_val - baseline_val) if baseline_val is not None else None

        label, higher_is_better = METRIC_META.get(metric, (metric, True))
        threshold = MEANINGFUL_DELTA.get(metric, 3.0)
        is_meaningful = abs(delta) >= threshold if delta is not None else False
        is_positive = (delta > 0) == higher_is_better if delta is not None else None

        # Convert weight to lbs for display
        if metric == "weight":
            display_baseline = round(baseline_val * KG_TO_LBS, 1) if baseline_val is not None else None
            display_during = round(during_val * KG_TO_LBS, 1)
            display_delta = round(delta * KG_TO_LBS, 1) if delta is not None else None
            unit = "lbs"
        elif metric == "hrv_ms":
            display_baseline = round(baseline_val, 0) if baseline_val is not None else None
            display_during = round(during_val, 0)
            display_delta = round(delta, 0) if delta is not None else None
            unit = "ms"
        elif metric in ("sleep_hours",):
            display_baseline = round(baseline_val, 1) if baseline_val is not None else None
            display_during = round(during_val, 1)
            display_delta = round(delta, 1) if delta is not None else None
            unit = "h"
        elif metric in ("recovery_score", "sleep_efficiency", "body_fat_pct"):
            display_baseline = round(baseline_val, 0) if baseline_val is not None else None
            display_during = round(during_val, 0)
            display_delta = round(delta, 0) if delta is not None else None
            unit = "%" if metric in ("sleep_efficiency", "body_fat_pct") else ""
        else:
            display_baseline = round(baseline_val, 1) if baseline_val is not None else None
            display_during = round(during_val, 1)
            display_delta = round(delta, 1) if delta is not None else None
            unit = ""

        metric_results.append({
            "metric": metric,
            "label": label,
            "unit": unit,
            "baseline": display_baseline,
            "during": display_during,
            "delta": display_delta,
            "meaningful": is_meaningful,
            "positive": is_positive,
        })

    if days_in < 5:
        maturity = "early"
    elif days_in < 14:
        maturity = "building"
    else:
        maturity = "established"

    return {
        "id": experiment.id,
        "name": experiment.name,
        "start_date": str(experiment.start_date),
        "days_in": days_in,
        "status": experiment.status,
        "maturity": maturity,
        "metrics": metric_results,
    }


def get_experiment_summaries(
    session: Session, user_id: int, as_of: date
) -> list[dict[str, Any]]:
    """Return result dicts for all non-archived experiments, active first."""
    experiments = session.scalars(
        select(Experiment)
        .where(Experiment.user_id == user_id, Experiment.status != "archived")
        .order_by(Experiment.status.desc(), Experiment.start_date.desc())
    ).all()
    return [compute_results(session, user_id, exp, as_of) for exp in experiments]


def format_experiment_text(result: dict[str, Any]) -> str:
    """Format a single experiment result for display in Telegram."""
    lines = []
    status_suffix = " (completed)" if result["status"] == "completed" else f" — Day {result['days_in']}"
    lines.append(f"🧪 *{result['name']}*{status_suffix}")

    if result["maturity"] == "early":
        lines.append("  _Still early — need a few more days._")
        return "\n".join(lines)

    for m in result["metrics"]:
        before = f"{m['baseline']}{m['unit']}" if m["baseline"] is not None else "no baseline"
        after = f"{m['during']}{m['unit']}"
        delta_str = ""
        if m["delta"] is not None:
            sign = "+" if m["delta"] > 0 else ""
            delta_str = f" ({sign}{m['delta']}{m['unit']})"
            if m["meaningful"] and m["positive"] is True:
                delta_str += " ↑"
            elif m["meaningful"] and m["positive"] is False:
                delta_str += " ↓"
        lines.append(f"  • {m['label']}: {before} → {after}{delta_str}")

    if result["maturity"] == "building":
        lines.append("  _Building signal — more data needed._")
    elif any(m["meaningful"] and m["positive"] for m in result["metrics"]):
        lines.append("  _Looking positive so far._")
    elif any(m["meaningful"] and m["positive"] is False for m in result["metrics"]):
        lines.append("  _No improvement detected yet._")
    else:
        lines.append("  _No clear signal yet._")

    return "\n".join(lines)
