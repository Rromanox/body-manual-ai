"""Unit 1: training_sessions + training_log models — roundtrip and defaults."""
from __future__ import annotations

from datetime import date

from app.models.training_log import TrainingLog
from app.models.training_session import TrainingSession
from tests.conftest import make_user


def test_training_session_defaults_and_roundtrip(mem_session):
    make_user(mem_session)
    s = TrainingSession(
        user_id=1,
        date=date(2026, 7, 14),
        week=1,
        phase="base",
        session_type="intervals",
        title="3x10 min Sweet Spot",
        details="15 min warm-up, 3x10 min SS w/ 5 min easy between, cool down",
        duration_min=60,
    )
    mem_session.add(s)
    mem_session.commit()

    row = mem_session.get(TrainingSession, s.id)
    assert row.loaded is False          # server_default
    assert row.priority == "normal"     # server_default
    assert row.status == "pending"      # server_default
    assert row.moved_from is None
    assert row.duration_min == 60


def test_training_log_json_detail_roundtrip(mem_session):
    make_user(mem_session)
    log = TrainingLog(
        user_id=1,
        session_date=date(2026, 7, 18),
        action="skipped",
        detail={"rule": "normal_no_reschedule", "reason": "rain"},
        source="command",
    )
    mem_session.add(log)
    mem_session.commit()

    row = mem_session.get(TrainingLog, log.id)
    assert row.detail == {"rule": "normal_no_reschedule", "reason": "rain"}
    assert row.action == "skipped"
    assert row.source == "command"
