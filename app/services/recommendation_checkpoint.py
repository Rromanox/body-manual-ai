"""Deterministic checkpoint evaluation for the recommendation ledger (Phase 3B).

The backend computes outcomes — never the AI. For each due pending recommendation
we compare the relevant metric and mark it improved / worsened / neutral /
inconclusive with a cautious summary. Conservative by design: missing data or an
unmeasurable metric is always "inconclusive", never a guess.

Pure evaluators (evaluate_recovery/_sleep/_strain) take plain numbers so they're
trivially testable; evaluate_due wires them to the DB via an injectable
metric_lookup (defaulting to a daily_metrics query) so the orchestration is
testable without a Postgres-only DailyMetric table.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.daily_metric import DailyMetric
from app.services import recommendation_ledger
from app.services.baseline_engine import STRAIN_HIGH, STRAIN_MODERATE

logger = logging.getLogger(__name__)

RECOVERY_DELTA = 5.0   # points to call recovery improved/worsened
SLEEP_SHORT = 0.5      # hours under target to call sleep worsened
WEIGHT_DELTA = 0.5     # lbs toward/away from target to call weight improved/worsened

# checkpoint_metric (as stored) -> canonical metric key
_METRIC_ALIASES = {
    "recovery": "recovery", "recovery_score": "recovery",
    "sleep": "sleep_hours", "sleep_hours": "sleep_hours",
    "strain": "strain",
    "weight": "weight", "weight_lbs": "weight",
}

# canonical metric key -> DailyMetric column
_METRIC_COLUMN = {
    "recovery": DailyMetric.recovery_score,
    "sleep_hours": DailyMetric.sleep_hours,
    "strain": DailyMetric.strain,
    "weight": DailyMetric.weight,
    "workout_count": DailyMetric.workout_count,
}

MetricLookup = Callable[[int, date, str], "float | None"]


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --- pure evaluators: return (outcome_status, followed_status|None, summary) ---

def evaluate_recovery(baseline: float | None, value: float | None) -> tuple[str, str | None, str]:
    if baseline is None or value is None:
        return ("inconclusive", None, "Could not evaluate — recovery data was missing.")
    diff = value - baseline
    if diff >= RECOVERY_DELTA:
        return ("improved", None,
                f"Recovery improved from {baseline:.0f} to {value:.0f} after this recommendation.")
    if diff <= -RECOVERY_DELTA:
        return ("worsened", None,
                f"Recovery dropped from {baseline:.0f} to {value:.0f}, so this may not have addressed the main driver.")
    return ("neutral", None, f"Recovery held about steady ({baseline:.0f} to {value:.0f}).")


def evaluate_sleep(target: float | None, value: float | None) -> tuple[str, str | None, str]:
    if target is None or value is None:
        return ("inconclusive", None, "Could not evaluate — sleep target or actual sleep was missing.")
    if value >= target:
        return ("improved", "followed", f"Slept {value:.1f}h, meeting the {target:.1f}h target.")
    if value < target - SLEEP_SHORT:
        return ("worsened", "not_followed", f"Slept {value:.1f}h, short of the {target:.1f}h target.")
    return ("neutral", "partial", f"Slept {value:.1f}h, close to the {target:.1f}h target.")


def evaluate_strain(limit: float | None, value: float | None) -> tuple[str, str | None, str]:
    if limit is None or value is None:
        return ("inconclusive", None, "Could not evaluate — strain limit or actual strain was missing.")
    if value <= limit:
        return ("neutral", "followed", f"Day strain was {value:.1f}, within the suggested limit of {limit:.0f}.")
    return ("neutral", "not_followed", f"Day strain was {value:.1f}, above the suggested limit of {limit:.0f}.")


def evaluate_weight(target: float | None, baseline: float | None, value: float | None) -> tuple[str, str | None, str]:
    # Conservative: only judge when there's an explicit target AND both readings.
    if target is None or baseline is None or value is None:
        return ("inconclusive", None, "Could not evaluate weight progress yet.")
    before_gap, after_gap = abs(baseline - target), abs(value - target)
    if after_gap <= before_gap - WEIGHT_DELTA:
        return ("improved", None, f"Weight moved toward the target ({baseline:.1f} to {value:.1f}).")
    if after_gap >= before_gap + WEIGHT_DELTA:
        return ("worsened", None, f"Weight moved away from the target ({baseline:.1f} to {value:.1f}).")
    return ("neutral", None, f"Weight held about steady ({baseline:.1f} to {value:.1f}).")


def _evaluate_one(rec, lookup: MetricLookup) -> tuple[str, str | None, str]:
    metric = _METRIC_ALIASES.get((rec.checkpoint_metric or "").lower())
    td = rec.trigger_data or {}
    if metric == "recovery":
        baseline = lookup(rec.user_id, rec.local_date, "recovery")
        value = lookup(rec.user_id, rec.checkpoint_date, "recovery")
        return evaluate_recovery(baseline, value)
    if metric == "sleep_hours":
        target = _num(td.get("target_hours") or td.get("target"))
        value = lookup(rec.user_id, rec.checkpoint_date, "sleep_hours")
        return evaluate_sleep(target, value)
    if metric == "strain":
        limit = _num(td.get("strain_limit") or td.get("target"))
        value = lookup(rec.user_id, rec.local_date, "strain")  # the day the limit applied
        return evaluate_strain(limit, value)
    if metric == "weight":
        target = _num(td.get("target") or td.get("target_lbs"))
        baseline = lookup(rec.user_id, rec.local_date, "weight")
        value = lookup(rec.user_id, rec.checkpoint_date, "weight")
        return evaluate_weight(target, baseline, value)
    return ("inconclusive", None, "Could not evaluate — no measurable checkpoint metric.")


def infer_followthrough(rec, lookup: MetricLookup) -> tuple[str | None, str | None]:
    """Infer follow-through from WHOOP data for training/sleep recs (Phase 3D).

    Returns (followed_status, cautious_note) or (None, None) when it can't be
    inferred (missing data, or a type WHOOP can't speak to like nutrition).
    Driven by trigger_data flags that extraction normalizes:
    strain_limit / avoid_workout / easy_day / target_hours.
    """
    td = rec.trigger_data or {}
    day = rec.local_date  # daytime activity applies to the recommendation day

    # Training — explicit strain limit
    limit = _num(td.get("strain_limit"))
    if limit is not None:
        strain = lookup(rec.user_id, day, "strain")
        if strain is None:
            return None, None
        if strain <= limit:
            return "followed", f"WHOOP shows day strain was {strain:.1f}, within the suggested limit of {limit:.0f}."
        return "not_followed", f"WHOOP shows day strain reached {strain:.1f}, above the suggested limit of {limit:.0f}."

    # Training — skip workout / easy day
    if td.get("avoid_workout") or td.get("easy_day"):
        workouts = lookup(rec.user_id, day, "workout_count")
        strain = lookup(rec.user_id, day, "strain")
        if workouts is None and strain is None:
            return None, None
        if (workouts or 0) > 0:
            return "not_followed", "WHOOP shows a workout was logged, so the easy-day recommendation wasn't followed."
        # no workout logged
        if strain is not None and strain >= STRAIN_HIGH:
            return "partial", "WHOOP shows no logged workout but a high-strain day, so the easy day was only partly followed."
        if strain is None or strain < STRAIN_MODERATE:
            return "followed", "WHOOP shows no workout and an easy day, consistent with the recommendation."
        return "partial", "WHOOP shows moderate activity, so the easy day was only partly followed."

    # Sleep — target hours (the night's sleep lands on the checkpoint/waking date)
    target_h = _num(td.get("target_hours"))
    if target_h is not None and rec.checkpoint_date is not None:
        sleep = lookup(rec.user_id, rec.checkpoint_date, "sleep_hours")
        if sleep is None:
            return None, None
        if sleep >= target_h:
            return "followed", f"WHOOP shows {sleep:.1f}h of sleep, meeting the {target_h:.1f}h target."
        if sleep < target_h - SLEEP_SHORT:
            return "not_followed", f"WHOOP shows {sleep:.1f}h of sleep, short of the {target_h:.1f}h target."
        return "partial", f"WHOOP shows {sleep:.1f}h of sleep, close to the {target_h:.1f}h target."

    return None, None


def _db_metric_lookup(session: Session) -> MetricLookup:
    def lookup(user_id: int, day: date, metric: str) -> float | None:
        col = _METRIC_COLUMN.get(metric)
        if col is None or day is None:
            return None
        val = session.scalar(
            select(col).where(
                DailyMetric.user_id == user_id,
                DailyMetric.date == day,
                col.is_not(None),
            )
        )
        return float(val) if val is not None else None
    return lookup


def evaluate_due(
    session: Session,
    user_id: int,
    as_of_date: date,
    *,
    metric_lookup: MetricLookup | None = None,
    commit: bool = True,
) -> dict[str, int]:
    """Evaluate every due, pending checkpoint for a user. Deterministic.

    Each recommendation is resolved exactly once (it leaves "pending" after), so
    re-running is safe and a no-op for already-checked rows. Returns counts.
    """
    lookup = metric_lookup or _db_metric_lookup(session)
    due = recommendation_ledger.get_due_checkpoints(session, user_id, as_of_date)
    checked = inconclusive = 0
    for rec in due:
        # Resolve follow-through: an explicit user mark wins; otherwise try to
        # infer it from WHOOP. This must happen BEFORE judging the advice.
        explicit = rec.followed_status != "unknown"
        followed = rec.followed_status
        follow_note: str | None = None
        if not explicit:
            inferred, follow_note = infer_followthrough(rec, lookup)
            if inferred is not None:
                followed = inferred

        # If it wasn't followed (said or shown), don't treat the outcome as a fair test.
        if followed == "not_followed":
            if explicit:
                summary = "Could not evaluate the recommendation because it appears it was not followed."
            else:
                summary = (follow_note or "WHOOP suggests this wasn't followed") + \
                    " I won't treat the outcome as a fair test of the advice."
            recommendation_ledger.mark_inconclusive(
                session, rec.id, outcome_summary=summary,
                followed_status=None if explicit else "not_followed", commit=False,
            )
            inconclusive += 1
            logger.info("recommendation checkpoint user=%s rec=%s outcome=inconclusive reason=not_followed explicit=%s", user_id, rec.id, explicit)
            continue

        # Followed / partial / still-unknown -> judge the outcome on the metric.
        outcome, auto_followed, summary = _evaluate_one(rec, lookup)
        if explicit and followed == "followed":
            summary = f"{summary} (You marked this as followed.)"
        elif follow_note:  # inferred from WHOOP
            summary = f"{follow_note} {summary}"

        # What follow-through to persist: explicit stays as-is; else inferred; else
        # whatever the metric evaluator could tell (e.g. strain/sleep comparison).
        if explicit:
            eff_followed = None
        elif followed != "unknown":
            eff_followed = followed
        else:
            eff_followed = auto_followed

        if outcome == "inconclusive":
            recommendation_ledger.mark_inconclusive(
                session, rec.id, outcome_summary=summary, followed_status=eff_followed, commit=False,
            )
            inconclusive += 1
        else:
            recommendation_ledger.mark_checked(
                session, rec.id, outcome_status=outcome,
                outcome_summary=summary, followed_status=eff_followed, commit=False,
            )
            checked += 1
        logger.info(
            "recommendation checkpoint user=%s rec=%s metric=%s outcome=%s followed=%s",
            user_id, rec.id, rec.checkpoint_metric, outcome, eff_followed,
        )
    if commit and due:
        session.commit()
    summary = {"due": len(due), "checked": checked, "inconclusive": inconclusive}
    if due:
        logger.info("recommendation checkpoints user=%s %s", user_id, summary)
    return summary
