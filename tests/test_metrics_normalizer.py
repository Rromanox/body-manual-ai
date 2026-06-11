"""Unit tests for the WHOOP cycle → local-calendar-day mapping.

These are the only required Week 1 tests (CLAUDE.md): the mapping is the easiest
thing in the project to get subtly wrong.
"""
from __future__ import annotations

from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

from app.services.metrics_normalizer import (
    normalize_whoop_data,
    waking_date_for_sleep,
)

TOKYO = ZoneInfo("Asia/Tokyo")
NEW_YORK = ZoneInfo("America/New_York")

MILLI_PER_HOUR = 3_600_000


def make_sleep(
    sleep_id: str = "sleep-1",
    start: str = "2026-06-09T03:30:00.000Z",
    end: str = "2026-06-09T11:10:00.000Z",
    nap: bool = False,
    score_state: str = "SCORED",
    light_milli: int = 4 * MILLI_PER_HOUR,
    sws_milli: int = 1 * MILLI_PER_HOUR,
    rem_milli: int = 2 * MILLI_PER_HOUR,
    efficiency: float = 91.0,
    performance: float = 85.0,
    consistency: float = 78.0,
    respiratory_rate: float = 14.2,
) -> dict[str, Any]:
    return {
        "id": sleep_id,
        "start": start,
        "end": end,
        "nap": nap,
        "score_state": score_state,
        "score": {
            "stage_summary": {
                "total_in_bed_time_milli": light_milli + sws_milli + rem_milli + MILLI_PER_HOUR // 2,
                "total_light_sleep_time_milli": light_milli,
                "total_slow_wave_sleep_time_milli": sws_milli,
                "total_rem_sleep_time_milli": rem_milli,
            },
            "sleep_efficiency_percentage": efficiency,
            "sleep_performance_percentage": performance,
            "sleep_consistency_percentage": consistency,
            "respiratory_rate": respiratory_rate,
        },
    }


def make_recovery(
    cycle_id: int = 101,
    sleep_id: str = "sleep-1",
    score_state: str = "SCORED",
    recovery_score: float = 67.0,
    resting_heart_rate: float = 54.0,
    hrv: float = 62.0,
) -> dict[str, Any]:
    return {
        "cycle_id": cycle_id,
        "sleep_id": sleep_id,
        "score_state": score_state,
        "score": {
            "recovery_score": recovery_score,
            "resting_heart_rate": resting_heart_rate,
            "hrv_rmssd_milli": hrv,
            "spo2_percentage": 96.5,
            "skin_temp_celsius": 33.7,
        },
    }


def make_cycle(
    cycle_id: int = 101,
    start: str = "2026-06-09T03:30:00.000Z",
    end: str | None = None,
    score_state: str = "SCORED",
    strain: float = 12.4,
) -> dict[str, Any]:
    return {
        "id": cycle_id,
        "start": start,
        "end": end,
        "score_state": score_state,
        "score": {"strain": strain},
    }


def make_workout(
    start: str = "2026-06-09T22:00:00.000Z",
    end: str = "2026-06-09T23:00:00.000Z",
) -> dict[str, Any]:
    return {"id": "workout-1", "start": start, "end": end, "score_state": "SCORED"}


def test_normal_night_maps_to_local_waking_date() -> None:
    # New York: in bed 23:30 June 8 ET, wake 07:10 June 9 ET
    sleep = make_sleep(start="2026-06-09T03:30:00.000Z", end="2026-06-09T11:10:00.000Z")
    rows = normalize_whoop_data([], [sleep], [], [], NEW_YORK)

    assert list(rows) == [date(2026, 6, 9)]
    row = rows[date(2026, 6, 9)]
    assert row.sleep_hours == 7.0  # light + SWS + REM, not in-bed time
    assert row.sleep_efficiency == 91.0
    assert row.respiratory_rate == 14.2


def test_positive_utc_offset_local_date_wins_over_utc_date() -> None:
    # Tokyo: wake 07:00 June 11 JST = 22:00 June 10 UTC. Local date must win.
    sleep = make_sleep(start="2026-06-10T14:00:00.000Z", end="2026-06-10T22:00:00.000Z")
    rows = normalize_whoop_data([], [sleep], [], [], TOKYO)

    assert list(rows) == [date(2026, 6, 11)]


def test_negative_utc_offset_local_date_wins_over_utc_date() -> None:
    # New York: sleep ends 03:30 June 10 UTC = 23:30 June 9 ET. Local date must win.
    sleep = make_sleep(start="2026-06-09T19:00:00.000Z", end="2026-06-10T03:30:00.000Z")
    rows = normalize_whoop_data([], [sleep], [], [], NEW_YORK)

    assert list(rows) == [date(2026, 6, 9)]


def test_nap_is_excluded_entirely() -> None:
    nap = make_sleep(
        sleep_id="nap-1",
        start="2026-06-09T17:00:00.000Z",
        end="2026-06-09T18:00:00.000Z",
        nap=True,
    )
    rows = normalize_whoop_data([], [nap], [], [], NEW_YORK)

    assert rows == {}


def test_nap_does_not_overwrite_primary_sleep_metrics() -> None:
    primary = make_sleep(start="2026-06-09T03:30:00.000Z", end="2026-06-09T11:10:00.000Z")
    nap = make_sleep(
        sleep_id="nap-1",
        start="2026-06-09T17:00:00.000Z",
        end="2026-06-09T18:00:00.000Z",
        nap=True,
        light_milli=MILLI_PER_HOUR,
        sws_milli=0,
        rem_milli=0,
    )
    rows = normalize_whoop_data([], [primary, nap], [], [], NEW_YORK)

    assert rows[date(2026, 6, 9)].sleep_hours == 7.0


def test_recovery_joins_to_waking_date_via_sleep_id() -> None:
    sleep = make_sleep()
    recovery = make_recovery()
    cycle = make_cycle()
    rows = normalize_whoop_data([cycle], [sleep], [recovery], [], NEW_YORK)

    row = rows[date(2026, 6, 9)]
    assert row.recovery_score == 67.0
    assert row.resting_heart_rate == 54.0
    assert row.hrv_ms == 62.0
    assert row.strain == 12.4


def test_missing_recovery_still_builds_row_from_sleep() -> None:
    sleep = make_sleep()
    rows = normalize_whoop_data([], [sleep], [], [], NEW_YORK)

    row = rows[date(2026, 6, 9)]
    assert row.recovery_score is None
    assert row.sleep_hours == 7.0


def test_unscored_records_contribute_no_metrics() -> None:
    sleep = make_sleep(score_state="PENDING_SCORE")
    recovery = make_recovery(score_state="PENDING_SCORE")
    cycle = make_cycle(score_state="PENDING_SCORE")
    rows = normalize_whoop_data([cycle], [sleep], [recovery], [], NEW_YORK)

    row = rows[date(2026, 6, 9)]
    assert row.sleep_hours is None
    assert row.recovery_score is None
    assert row.strain is None


def test_strain_lands_on_waking_date_not_cycle_start_date() -> None:
    # Tokyo: cycle starts 22:30 June 8 JST (13:30 UTC); its recovery's sleep ends
    # 07:00 June 9 JST. Strain must land on June 9, the waking date.
    sleep = make_sleep(start="2026-06-08T13:30:00.000Z", end="2026-06-08T22:00:00.000Z")
    recovery = make_recovery()
    cycle = make_cycle(start="2026-06-08T13:30:00.000Z", strain=15.1)
    rows = normalize_whoop_data([cycle], [sleep], [recovery], [], TOKYO)

    assert list(rows) == [date(2026, 6, 9)]
    assert rows[date(2026, 6, 9)].strain == 15.1


def test_cycle_without_recovery_falls_back_to_cycle_start_local_date() -> None:
    cycle = make_cycle(start="2026-06-09T11:30:00.000Z", strain=4.2)  # 07:30 ET June 9
    rows = normalize_whoop_data([cycle], [], [], [], NEW_YORK)

    assert list(rows) == [date(2026, 6, 9)]
    assert rows[date(2026, 6, 9)].strain == 4.2


def test_late_evening_workout_maps_to_its_own_local_date() -> None:
    # 22:00–23:00 June 9 ET = 02:00–03:00 June 10 UTC
    workout = make_workout(start="2026-06-10T02:00:00.000Z", end="2026-06-10T03:00:00.000Z")
    rows = normalize_whoop_data([], [], [], [workout], NEW_YORK)

    row = rows[date(2026, 6, 9)]
    assert row.workout_count == 1
    assert row.total_workout_minutes == 60.0


def test_dst_spring_forward_night() -> None:
    # US DST starts 2026-03-08. In bed 23:00 March 7 EST, wake 07:00 March 8 EDT.
    sleep = make_sleep(start="2026-03-08T04:00:00.000Z", end="2026-03-08T11:00:00.000Z")
    rows = normalize_whoop_data([], [sleep], [], [], NEW_YORK)

    assert list(rows) == [date(2026, 3, 8)]


def test_multi_day_window_produces_distinct_rows() -> None:
    night1 = make_sleep(sleep_id="s1", start="2026-06-08T03:30:00.000Z", end="2026-06-08T11:00:00.000Z")
    night2 = make_sleep(sleep_id="s2", start="2026-06-09T03:30:00.000Z", end="2026-06-09T11:10:00.000Z")
    rec1 = make_recovery(cycle_id=100, sleep_id="s1", recovery_score=80.0)
    rec2 = make_recovery(cycle_id=101, sleep_id="s2", recovery_score=55.0)
    cyc1 = make_cycle(cycle_id=100, start="2026-06-08T11:00:00.000Z", strain=9.0)
    cyc2 = make_cycle(cycle_id=101, start="2026-06-09T11:10:00.000Z", strain=13.0)
    rows = normalize_whoop_data([cyc1, cyc2], [night1, night2], [rec1, rec2], [], NEW_YORK)

    assert sorted(rows) == [date(2026, 6, 8), date(2026, 6, 9)]
    assert rows[date(2026, 6, 8)].recovery_score == 80.0
    assert rows[date(2026, 6, 8)].strain == 9.0
    assert rows[date(2026, 6, 9)].recovery_score == 55.0
    assert rows[date(2026, 6, 9)].strain == 13.0


def test_two_sleeps_same_waking_date_keeps_longer_one() -> None:
    short = make_sleep(
        sleep_id="short",
        start="2026-06-09T08:00:00.000Z",
        end="2026-06-09T10:00:00.000Z",
        light_milli=2 * MILLI_PER_HOUR,
        sws_milli=0,
        rem_milli=0,
        performance=40.0,
    )
    long = make_sleep(sleep_id="long", start="2026-06-08T22:00:00.000Z", end="2026-06-09T06:00:00.000Z")
    rows = normalize_whoop_data([], [short, long], [], [], NEW_YORK)

    row = rows[date(2026, 6, 9)]
    assert row.sleep_hours == 7.0
    assert row.sleep_performance == 85.0


def test_waking_date_helper_uses_sleep_end() -> None:
    sleep = make_sleep(start="2026-06-10T14:00:00.000Z", end="2026-06-10T22:00:00.000Z")
    assert waking_date_for_sleep(sleep, TOKYO) == date(2026, 6, 11)
    assert waking_date_for_sleep(sleep, NEW_YORK) == date(2026, 6, 10)
