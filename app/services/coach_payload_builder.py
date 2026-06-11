"""Assembles the structured JSON payload the AI narrates (SPEC §Daily Coach Message).

Pre-computed conclusions only. The AI never sees raw history and never does
arithmetic, so every number here is already rounded and every comparison is
already a flag.
"""
from __future__ import annotations

from typing import Any

from app.models.user import User
from app.services.baseline_engine import DailySnapshot, MetricSummary


def build_daily_payload(user: User, snapshot: DailySnapshot) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "user_goal": user.goal or "general_health",
        "data_days_available": snapshot.data_days_available,
        "data_maturity": snapshot.data_maturity,
        "today_recovery_missing": snapshot.recovery.today is None,
    }
    for key, summary in (
        ("recovery", snapshot.recovery),
        ("sleep_hours", snapshot.sleep_hours),
        ("resting_hr", snapshot.resting_hr),
        ("hrv", snapshot.hrv),
    ):
        block = _metric_block(summary)
        if block is not None:
            payload[key] = block
    if snapshot.yesterday_strain is not None:
        payload["yesterday_strain"] = snapshot.yesterday_strain
    if snapshot.yesterday_workout_count:
        payload["yesterday_workouts"] = {
            "count": snapshot.yesterday_workout_count,
            "minutes": _round1(snapshot.yesterday_workout_minutes),
        }
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
