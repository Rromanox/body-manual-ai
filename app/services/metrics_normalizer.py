"""Maps WHOOP v2 records onto the user's local calendar days.

`daily_metrics.date` is the local date the user WOKE UP (CLAUDE.md gotcha 1),
computed with `users.timezone` — not the record's `timezone_offset`.

The waking event is the end of the primary (non-nap) sleep. Recovery and strain
are anchored through the recovery's `sleep_id` join, so the mapping holds
regardless of whether WHOOP cycles run wake-to-wake or sleep-onset-to-sleep-onset.

Pure functions only — no I/O — so the unit tests need neither a DB nor HTTP.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

MILLI_PER_HOUR = 3_600_000

# DailyRow fields that map 1:1 onto daily_metrics columns
WHOOP_METRIC_FIELDS = (
    "recovery_score",
    "hrv_ms",
    "resting_heart_rate",
    "respiratory_rate",
    "spo2",
    "skin_temp",
    "sleep_hours",
    "sleep_efficiency",
    "sleep_performance",
    "sleep_consistency",
    "strain",
    "workout_count",
    "total_workout_minutes",
)


@dataclass
class DailyRow:
    """Draft of one daily_metrics row. None means 'no data', never 'zero'."""

    recovery_score: float | None = None
    hrv_ms: float | None = None
    resting_heart_rate: float | None = None
    respiratory_rate: float | None = None
    spo2: float | None = None
    skin_temp: float | None = None
    sleep_hours: float | None = None
    sleep_efficiency: float | None = None
    sleep_performance: float | None = None
    sleep_consistency: float | None = None
    strain: float | None = None
    workout_count: int | None = None
    total_workout_minutes: float | None = None
    raw: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {"cycles": [], "sleeps": [], "recoveries": [], "workouts": []}
    )


def parse_whoop_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def local_date(value: str, tz: ZoneInfo) -> date:
    return parse_whoop_timestamp(value).astimezone(tz).date()


def waking_date_for_sleep(sleep: dict[str, Any], tz: ZoneInfo) -> date:
    return local_date(sleep["end"], tz)


def normalize_whoop_data(
    cycles: list[dict[str, Any]],
    sleeps: list[dict[str, Any]],
    recoveries: list[dict[str, Any]],
    workouts: list[dict[str, Any]],
    tz: ZoneInfo,
) -> dict[date, DailyRow]:
    rows: dict[date, DailyRow] = {}
    sleeps_by_id = {s["id"]: s for s in sleeps if "id" in s}
    recovery_by_cycle = {r["cycle_id"]: r for r in recoveries if "cycle_id" in r}
    # in-bed duration of the sleep currently backing each row's sleep_* fields,
    # so a longer primary sleep wins if two non-nap sleeps share a waking date
    chosen_sleep_milli: dict[date, int] = {}

    for sleep in sleeps:
        if sleep.get("nap") or not sleep.get("end"):
            continue
        day = waking_date_for_sleep(sleep, tz)
        row = rows.setdefault(day, DailyRow())
        row.raw["sleeps"].append(sleep)
        if sleep.get("score_state") != "SCORED":
            continue
        score = sleep.get("score") or {}
        duration_milli = _sleep_duration_milli(sleep, score)
        if day in chosen_sleep_milli and duration_milli <= chosen_sleep_milli[day]:
            continue
        chosen_sleep_milli[day] = duration_milli
        row.sleep_hours = _asleep_hours(score)
        row.sleep_efficiency = score.get("sleep_efficiency_percentage")
        row.sleep_performance = score.get("sleep_performance_percentage")
        row.sleep_consistency = score.get("sleep_consistency_percentage")
        row.respiratory_rate = score.get("respiratory_rate")

    for recovery in recoveries:
        sleep = sleeps_by_id.get(recovery.get("sleep_id"))
        if sleep is None or not sleep.get("end"):
            # sleep fell outside the pull window — the next overlapping pull catches it
            continue
        day = waking_date_for_sleep(sleep, tz)
        row = rows.setdefault(day, DailyRow())
        row.raw["recoveries"].append(recovery)
        if recovery.get("score_state") != "SCORED":
            continue
        score = recovery.get("score") or {}
        row.recovery_score = score.get("recovery_score")
        row.resting_heart_rate = score.get("resting_heart_rate")
        row.hrv_ms = score.get("hrv_rmssd_milli")
        row.spo2 = score.get("spo2_percentage")
        row.skin_temp = score.get("skin_temp_celsius")

    for cycle in cycles:
        if not cycle.get("start"):
            continue
        recovery = recovery_by_cycle.get(cycle.get("id"))
        sleep = sleeps_by_id.get(recovery.get("sleep_id")) if recovery else None
        if sleep is not None and sleep.get("end"):
            day = waking_date_for_sleep(sleep, tz)
        else:
            # no recovery to anchor on (first-ever cycle, or not yet scored):
            # the cycle start is the best available approximation of the strain day
            day = local_date(cycle["start"], tz)
        row = rows.setdefault(day, DailyRow())
        row.raw["cycles"].append(cycle)
        if cycle.get("score_state") != "SCORED":
            continue
        score = cycle.get("score") or {}
        if score.get("strain") is not None:
            row.strain = score["strain"]

    for workout in workouts:
        if not workout.get("start"):
            continue
        # workouts happen while awake — their own local date is the right day,
        # even though the next waking date would be tomorrow
        day = local_date(workout["start"], tz)
        row = rows.setdefault(day, DailyRow())
        row.raw["workouts"].append(workout)
        row.workout_count = (row.workout_count or 0) + 1
        if workout.get("end"):
            minutes = (
                parse_whoop_timestamp(workout["end"]) - parse_whoop_timestamp(workout["start"])
            ).total_seconds() / 60
            row.total_workout_minutes = round((row.total_workout_minutes or 0.0) + minutes, 1)

    return rows


def _sleep_duration_milli(sleep: dict[str, Any], score: dict[str, Any]) -> int:
    stage_summary = score.get("stage_summary") or {}
    in_bed = stage_summary.get("total_in_bed_time_milli")
    if in_bed is not None:
        return int(in_bed)
    delta = parse_whoop_timestamp(sleep["end"]) - parse_whoop_timestamp(sleep["start"])
    return int(delta.total_seconds() * 1000)


def _asleep_hours(score: dict[str, Any]) -> float | None:
    stage_summary = score.get("stage_summary") or {}
    stages = (
        stage_summary.get("total_light_sleep_time_milli"),
        stage_summary.get("total_slow_wave_sleep_time_milli"),
        stage_summary.get("total_rem_sleep_time_milli"),
    )
    if all(value is None for value in stages):
        return None
    return round(sum(value or 0 for value in stages) / MILLI_PER_HOUR, 2)
