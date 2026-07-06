"""Unit 2: training_plan service — calendar helpers, reads, audit log, writes."""
from __future__ import annotations

from datetime import date

import pytest

from app.models.training_log import TrainingLog
from app.services import training_plan as tp
from tests.conftest import make_user


def _seed_min(session, user_id=1):
    """A tiny slice: two sessions in week 1 + one rest day + one critical ride."""
    tp.upsert_session(
        session, user_id, date(2026, 7, 14), week=1, phase="base",
        session_type="intervals", title="3x10 SS", duration_min=60,
    )
    tp.upsert_session(
        session, user_id, date(2026, 7, 18), week=1, phase="base",
        session_type="long_ride", title="90 min endurance", duration_min=90,
        priority="high",
    )
    tp.upsert_session(
        session, user_id, date(2026, 7, 13), week=1, phase="base",
        session_type="rest", title="Rest",
    )
    tp.upsert_session(
        session, user_id, date(2026, 9, 19), week=10, phase="specificity",
        session_type="long_ride", title="Biggest ride", duration_min=240,
        loaded=True, priority="critical",
    )


def test_week_and_phase_helpers():
    assert tp.week_of(date(2026, 7, 13)) == 1        # Monday, plan start
    assert tp.week_of(date(2026, 7, 19)) == 1        # Sunday of week 1
    assert tp.week_of(date(2026, 7, 20)) == 2        # Monday of week 2
    assert tp.week_of(date(2026, 10, 4)) == 12       # last day
    assert tp.week_of(date(2026, 7, 12)) is None     # before plan
    assert tp.week_of(date(2026, 10, 5)) is None     # after plan
    assert tp.phase_for_week(1) == "base"
    assert tp.phase_for_week(8) == "build"
    assert tp.phase_for_week(9) == "specificity"
    assert tp.phase_for_week(12) == "taper"
    assert tp.week_date_range(1) == (date(2026, 7, 13), date(2026, 7, 19))


def test_get_week_returns_full_week_ordered(mem_session):
    make_user(mem_session)
    _seed_min(mem_session)
    week1 = tp.get_week(mem_session, 1, 1)
    assert [s.date for s in week1] == [date(2026, 7, 13), date(2026, 7, 14), date(2026, 7, 18)]


def test_upsert_is_idempotent_and_preserves_status(mem_session):
    make_user(mem_session)
    tp.upsert_session(
        mem_session, 1, date(2026, 7, 14), week=1, phase="base",
        session_type="intervals", title="3x10 SS", duration_min=60,
    )
    tp.mark_completed(mem_session, 1, date(2026, 7, 14))
    # Re-seed the same date: definition refreshes, status stays completed.
    tp.upsert_session(
        mem_session, 1, date(2026, 7, 14), week=1, phase="base",
        session_type="intervals", title="3x10 SS (v2)", duration_min=65,
    )
    rows = tp.get_week(mem_session, 1, 1)
    assert len(rows) == 1
    assert rows[0].title == "3x10 SS (v2)"
    assert rows[0].duration_min == 65
    assert rows[0].status == "completed"


def test_plan_overview_counts(mem_session):
    make_user(mem_session)
    _seed_min(mem_session)
    tp.mark_completed(mem_session, 1, date(2026, 7, 14))
    ov = tp.plan_overview(mem_session, 1, date(2026, 7, 15))
    # non-rest sessions: intervals, long_ride(high), long_ride(critical) = 3; 1 done
    assert ov["total_sessions"] == 3
    assert ov["completed_sessions"] == 1
    assert ov["completion_pct"] == 33
    assert ov["current_week"] == 1
    assert ov["current_phase"] == "base"
    assert ov["critical_total"] == 1
    assert ov["critical_done"] == 0
    assert ov["critical_remaining"] == 1


def test_mark_completed_logs_and_skips_rest(mem_session):
    make_user(mem_session)
    _seed_min(mem_session)
    assert tp.mark_completed(mem_session, 1, date(2026, 7, 13)) is None   # rest day
    assert tp.mark_completed(mem_session, 1, date(2026, 7, 15)) is None   # no session
    row = tp.mark_completed(mem_session, 1, date(2026, 7, 14), notes="felt great")
    assert row.status == "completed"
    assert row.completed_notes == "felt great"
    logs = mem_session.query(TrainingLog).filter_by(action="completed").all()
    assert len(logs) == 1 and logs[0].session_date == date(2026, 7, 14)


def test_log_action_validates(mem_session):
    make_user(mem_session)
    with pytest.raises(ValueError):
        tp.log_action(mem_session, 1, action="bogus", source="command")
    with pytest.raises(ValueError):
        tp.log_action(mem_session, 1, action="completed", source="bogus")
