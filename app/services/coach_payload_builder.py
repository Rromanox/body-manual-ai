"""Assembles the structured JSON payload the AI narrates (SPEC §Daily Coach Message).

Pre-computed conclusions only. The AI never sees raw history and never does
arithmetic, so every number here is already rounded and every comparison is
already a flag.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.models.daily_metric import DailyMetric
from app.models.user import User
from app.services.baseline_engine import DailySnapshot, MetricSummary, QAContext, WeeklySnapshot, get_checkin_streak
from app.services.timekit import now_block


def build_daily_payload(
    user: User,
    snapshot: DailySnapshot,
    yesterday_tags: list[str] | None = None,
    today_metric_row: DailyMetric | None = None,
    checkin_streak: int = 0,
    now: datetime | None = None,
    previous_message: str | None = None,
    closed_loops: list[dict[str, Any]] | None = None,
    gap_fill_question: bool = False,
    commitments: list[dict] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "now": now_block(user, now),
        "user_name": user.first_name or None,
        "user_goal": user.goal or "general_health",
        "data_days_available": snapshot.data_days_available,
        "data_maturity": snapshot.data_maturity,
        "today_recovery_missing": snapshot.recovery.today is None,
    }
    if previous_message:
        payload["previous_message"] = previous_message
    if checkin_streak >= 3:
        payload["checkin_streak"] = checkin_streak
    for key, summary in (
        ("recovery", snapshot.recovery),
        ("sleep_hours", snapshot.sleep_hours),
        ("resting_hr", snapshot.resting_hr),
        ("hrv", snapshot.hrv),
    ):
        block = _metric_block(summary)
        if block is not None:
            payload[key] = block
    yesterday_metrics: dict[str, Any] = {}
    for key, value in (
        ("recovery", snapshot.yesterday_recovery),
        ("sleep_hours", snapshot.yesterday_sleep_hours),
        ("resting_hr", snapshot.yesterday_resting_hr),
        ("hrv", snapshot.yesterday_hrv),
    ):
        if value is not None:
            yesterday_metrics[key] = _round1(value)
    if yesterday_metrics:
        payload["yesterday"] = yesterday_metrics
    if snapshot.yesterday_strain is not None:
        payload["yesterday_strain"] = snapshot.yesterday_strain
    if snapshot.yesterday_workout_count:
        payload["yesterday_workouts"] = {
            "count": snapshot.yesterday_workout_count,
            "minutes": _round1(snapshot.yesterday_workout_minutes),
        }
    if yesterday_tags:
        payload["yesterday_tags"] = yesterday_tags
    if today_metric_row is not None:
        if today_metric_row.sleep_start_local:
            payload["sleep_start"] = today_metric_row.sleep_start_local
        if today_metric_row.sleep_end_local:
            payload["sleep_end"] = today_metric_row.sleep_end_local
        for stage_key, col in (
            ("rem_hours", "rem_sleep_hours"),
            ("deep_hours", "deep_sleep_hours"),
            ("light_hours", "light_sleep_hours"),
        ):
            val = getattr(today_metric_row, col, None)
            if val is not None:
                payload.setdefault("sleep_stages", {})[stage_key] = _round1(val)
        body_comp: dict[str, Any] = {}
        if today_metric_row.weight is not None:
            body_comp["weight_lbs"] = _round1(_kg_to_lbs(today_metric_row.weight))
        if today_metric_row.body_fat_pct is not None:
            body_comp["body_fat_pct"] = _round1(today_metric_row.body_fat_pct)
        if today_metric_row.muscle_mass is not None:
            body_comp["muscle_mass_lbs"] = _round1(_kg_to_lbs(today_metric_row.muscle_mass))
        if today_metric_row.fat_free_mass is not None:
            body_comp["fat_free_mass_lbs"] = _round1(_kg_to_lbs(today_metric_row.fat_free_mass))
        if today_metric_row.water_pct is not None:
            body_comp["hydration_pct"] = _round1(today_metric_row.water_pct)
        if today_metric_row.bone_mass is not None:
            body_comp["bone_mass_lbs"] = _round1(_kg_to_lbs(today_metric_row.bone_mass))
        if body_comp:
            payload["body_composition"] = body_comp

    # Weight trend is independent of whether today has a body comp row
    # (it's computed from the last 16 days of weight history)
    if snapshot.weight_trend is not None:
        wt = snapshot.weight_trend
        weight_block: dict[str, Any] = {}
        if wt.overnight_change_lbs is not None:
            weight_block["overnight_change_lbs"] = wt.overnight_change_lbs
        if wt.weekly_trend_lbs is not None:
            weight_block["trend_per_week_lbs"] = wt.weekly_trend_lbs
        if wt.flag:
            weight_block["flag"] = wt.flag
        if weight_block:
            payload.setdefault("body_composition", {})["weight_trend"] = weight_block

    if closed_loops:
        payload["closed_loops"] = closed_loops
    if gap_fill_question:
        payload["gap_fill_question"] = True
    if commitments:
        payload["commitments"] = commitments[:2]

    return payload


def build_weekly_payload(
    user: User, snapshot: WeeklySnapshot, now: datetime | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "now": now_block(user, now),
        "user_goal": user.goal or "general_health",
        "period_days": 7,
        "data_days_available": snapshot.data_days_available,
        "data_maturity": snapshot.data_maturity,
    }
    for key, summary in (
        ("recovery", snapshot.recovery),
        ("sleep_hours", snapshot.sleep_hours),
        ("resting_hr", snapshot.resting_hr),
        ("hrv", snapshot.hrv),
    ):
        if summary.today is not None or summary.baseline_30d is not None:
            block: dict[str, Any] = {}
            if summary.today is not None:
                block["this_week_avg"] = _round1(summary.today)
            if summary.baseline_30d is not None:
                block["baseline_30d"] = _round1(summary.baseline_30d)
            if summary.flag:
                block["trend"] = summary.flag
            payload[key] = block
    if snapshot.avg_strain_7d is not None:
        payload["avg_strain_7d"] = _round1(snapshot.avg_strain_7d)
    return payload


def build_qa_payload(
    question: str, context: QAContext, now: dict[str, Any] | None = None
) -> dict[str, Any]:
    def _r(v: float | None) -> float | None:
        return round(v, 1) if v is not None else None

    payload: dict[str, Any] = {
        "question": question,
        "now": now or {},
        "user_name": context.user_name or None,
        "today_date": context.today_date,
        "data_days_available": context.data_days_available,
        "data_maturity": context.data_maturity,
        "recent_daily_data": context.recent_daily_data,
        "averages_last_7_days": {k: _r(v) for k, v in context.avg_7d.items()},
        "averages_last_30_days": {k: _r(v) for k, v in context.avg_30d.items()},
        "recent_tags_last_7_days": context.recent_tags,
        "observations": context.observations,
    }
    if context.max_heart_rate:
        payload["max_heart_rate"] = context.max_heart_rate
    if context.height_meter:
        payload["height_meter"] = _round1(context.height_meter)
    if context.user_goal:
        payload["user_goal"] = context.user_goal
    if context.recent_events:
        payload["recent_logs"] = context.recent_events
    if context.supplement_history:
        payload["supplement_history"] = context.supplement_history
    if context.coach_notes:
        payload["about_you"] = context.coach_notes
    return payload


def _metric_block(summary: MetricSummary) -> dict[str, Any] | None:
    if summary.today is None and summary.baseline_30d is None:
        return None
    block: dict[str, Any] = {}
    if summary.today is not None:
        block["today"] = _round1(summary.today)
    if summary.baseline_7d is not None:
        block["baseline_7d"] = _round1(summary.baseline_7d)
    if summary.baseline_30d is not None:
        block["baseline_30d"] = _round1(summary.baseline_30d)
    if summary.flag is not None:
        block["flag"] = summary.flag
    return block


def _round1(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None


def _kg_to_lbs(kg: float) -> float:
    return kg * 2.20462
