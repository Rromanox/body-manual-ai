"""Unit 5: recovery gate — zone boundaries, HRV/RHR logic, missing data, fueling."""
from __future__ import annotations

from datetime import date, timedelta

from app.models.daily_metric import DailyMetric
from app.models.training_log import TrainingLog
from app.services import training_gate as gate
from app.services import training_plan as tp
from tests.conftest import make_user


def _dm(session, d, *, recovery=None, hrv=None, rhr=None, weight=None, user_id=1):
    session.add(DailyMetric(
        user_id=user_id, date=d, recovery_score=recovery,
        hrv_ms=hrv, resting_heart_rate=rhr, weight=weight,
    ))
    session.commit()


def _intervals(session, d, user_id=1):
    tp.upsert_session(
        session, user_id, d, week=tp.week_of(d) or 1, phase="base",
        session_type="intervals", title="3x10 SS", duration_min=60,
    )


DAY = date(2026, 7, 14)  # Tuesday, week 1


def test_zone_boundaries(mem_session):
    make_user(mem_session)
    _intervals(mem_session, DAY)
    for rec, expected in [(67, "green"), (66, "yellow"), (34, "yellow"), (33, "red")]:
        mem_session.query(DailyMetric).delete()
        mem_session.commit()
        _dm(mem_session, DAY, recovery=rec)
        g = gate.evaluate_gate(mem_session, 1, DAY)
        assert g.zone == expected, f"recovery {rec} -> {g.zone}, expected {expected}"


def test_green_is_as_written_yellow_offers_adjustment(mem_session):
    make_user(mem_session)
    _intervals(mem_session, DAY)
    _dm(mem_session, DAY, recovery=80)
    assert gate.evaluate_gate(mem_session, 1, DAY).adjustment_offered is False
    mem_session.query(DailyMetric).delete(); mem_session.commit()
    _dm(mem_session, DAY, recovery=50)
    g = gate.evaluate_gate(mem_session, 1, DAY)
    assert g.zone == "yellow" and g.adjustment_offered and "tempo" in g.adjustment


def test_hrv_two_day_low_forces_red(mem_session):
    make_user(mem_session)
    _intervals(mem_session, DAY)
    # 14-day HRV baseline ~100.
    for i in range(2, 15):
        _dm(mem_session, DAY - timedelta(days=i), hrv=100)
    _dm(mem_session, DAY - timedelta(days=1), recovery=70, hrv=80)  # yesterday low
    _dm(mem_session, DAY, recovery=70, hrv=80)                       # today low
    g = gate.evaluate_gate(mem_session, 1, DAY)
    assert g.hrv_2day_low is True
    assert g.zone == "red"  # despite recovery 70


def test_hrv_single_day_low_not_red(mem_session):
    make_user(mem_session)
    _intervals(mem_session, DAY)
    for i in range(2, 15):
        _dm(mem_session, DAY - timedelta(days=i), hrv=100)
    _dm(mem_session, DAY - timedelta(days=1), recovery=70, hrv=100)  # yesterday normal
    _dm(mem_session, DAY, recovery=70, hrv=80)                       # only today low
    g = gate.evaluate_gate(mem_session, 1, DAY)
    assert g.hrv_2day_low is False
    assert g.zone == "green"


def test_rhr_three_day_alarm(mem_session):
    make_user(mem_session)
    _intervals(mem_session, DAY)
    for i in range(3, 15):
        _dm(mem_session, DAY - timedelta(days=i), rhr=50)   # baseline ~50
    _dm(mem_session, DAY - timedelta(days=2), recovery=80, rhr=60)
    _dm(mem_session, DAY - timedelta(days=1), recovery=80, rhr=60)
    _dm(mem_session, DAY, recovery=80, rhr=60)
    g = gate.evaluate_gate(mem_session, 1, DAY)
    assert g.rhr_alarm is True
    assert any("resting heart rate" in n for n in g.notes)


def test_rhr_two_days_not_alarm(mem_session):
    make_user(mem_session)
    _intervals(mem_session, DAY)
    for i in range(3, 15):
        _dm(mem_session, DAY - timedelta(days=i), rhr=50)
    _dm(mem_session, DAY - timedelta(days=1), recovery=80, rhr=60)
    _dm(mem_session, DAY, recovery=80, rhr=60)
    assert gate.evaluate_gate(mem_session, 1, DAY).rhr_alarm is False


def test_missing_data_shows_as_written_never_reuses(mem_session):
    make_user(mem_session)
    _intervals(mem_session, DAY)
    _dm(mem_session, DAY - timedelta(days=1), recovery=80)  # yesterday had a score
    # No row for today at all.
    g = gate.evaluate_gate(mem_session, 1, DAY)
    assert g.zone == "no_data"
    assert g.recovery is None
    assert g.adjustment_offered is False
    assert "as written" in g.recovery_line


def test_saturday_long_ride_yellow_exception(mem_session):
    make_user(mem_session)
    sat = date(2026, 7, 18)  # Saturday
    tp.upsert_session(
        mem_session, 1, sat, week=1, phase="base",
        session_type="long_ride", title="90 min endurance", duration_min=90, priority="high",
    )
    _dm(mem_session, sat, recovery=50)  # yellow
    assert gate.evaluate_gate(mem_session, 1, sat).adjustment_offered is False  # as written
    mem_session.query(DailyMetric).delete(); mem_session.commit()
    _dm(mem_session, sat, recovery=30)  # red
    g = gate.evaluate_gate(mem_session, 1, sat)
    assert g.zone == "red" and g.adjustment_offered  # only modified on red


def test_accept_and_override(mem_session):
    make_user(mem_session)
    _intervals(mem_session, DAY)
    _dm(mem_session, DAY, recovery=50)
    g = gate.evaluate_gate(mem_session, 1, DAY)
    gate.record_recommendation(mem_session, 1, DAY, g)
    row = tp.get_session(mem_session, 1, DAY)
    assert row.recovery_adjustment and "tempo" in row.recovery_adjustment

    gate.accept_adjustment(mem_session, 1, DAY)
    assert tp.get_session(mem_session, 1, DAY).status == "modified"
    actions = {l.action for l in mem_session.query(TrainingLog).all()}
    assert "gate_recommendation" in actions and "gate_accepted" in actions

    gate.override_adjustment(mem_session, 1, DAY)
    assert any(l.action == "gate_overridden" for l in mem_session.query(TrainingLog).all())


def test_fueling_flag_on_week5_sunday(mem_session):
    make_user(mem_session)
    sunday = date(2026, 8, 16)  # week 5 Sunday
    assert tp.week_of(sunday) == 5 and sunday.weekday() == 6
    for i in range(0, 7):        # recent week ~79 kg
        _dm(mem_session, sunday - timedelta(days=i), weight=79.0)
    for i in range(7, 14):       # prior week ~80 kg
        _dm(mem_session, sunday - timedelta(days=i), weight=80.0)
    flag = gate.fueling_flag(mem_session, 1, sunday)
    assert flag and flag["flag"] == "under_fueling"
    assert flag["weekly_lbs"] < -1.0

    # Not a fueling Sunday (week 3) -> None.
    assert gate.fueling_flag(mem_session, 1, date(2026, 8, 2)) is None
