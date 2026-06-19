"""Phase 2B: memory-aware prompt activation.

Prompts are module constants — assert they carry the memory rules. Payload
builders are checked for conditional memory_context inclusion (present when given,
absent when not), so existing memory-free behavior is provably preserved.
"""
from __future__ import annotations

from datetime import date

from app.models.user import User
from app.services import ai_client
from app.services.baseline_engine import (
    DailySnapshot,
    MetricSummary,
    QAContext,
    WeeklySnapshot,
)
from app.services.coach_payload_builder import (
    build_daily_payload,
    build_qa_payload,
    build_weekly_payload,
)

_MEM = [{"id": 1, "type": "preference", "content": "Prefers blunt coaching", "confidence": "high"}]


def _user() -> User:
    return User(telegram_id=1, timezone="America/Detroit", first_name="T", goal="general_health")


def _metric() -> MetricSummary:
    return MetricSummary(today=None, baseline_7d=None, baseline_30d=None, flag=None)


def _daily_snapshot() -> DailySnapshot:
    m = _metric()
    return DailySnapshot(
        target_date=date(2026, 6, 17),
        recovery=m, sleep_hours=m, resting_hr=m, hrv=m,
        yesterday_strain=None, yesterday_workout_count=None, yesterday_workout_minutes=None,
        data_days_available=10, data_maturity="established", safety_triggers=[],
    )


def _weekly_snapshot() -> WeeklySnapshot:
    m = _metric()
    return WeeklySnapshot(
        recovery=m, sleep_hours=m, resting_hr=m, hrv=m,
        avg_strain_7d=None, data_days_available=30, data_maturity="established",
    )


def _qa_context() -> QAContext:
    return QAContext(
        data_days_available=10, data_maturity="established",
        avg_7d={}, avg_30d={}, recent_tags=[], observations=[],
        recent_daily_data=[], today_date="2026-06-17",
    )


# --- prompt content ---------------------------------------------------------

def test_qa_prompt_activates_memory():
    p = ai_client.QA_SYSTEM_PROMPT
    assert "memory_context" in p
    assert "Using memory_context" in p
    assert "low-confidence" in p
    assert "silently" in p.lower()
    # current data must win over memory
    assert "data always wins" in p.lower() or "trust the numbers" in p.lower()


def test_morning_prompt_activates_memory():
    p = ai_client.SYSTEM_PROMPT
    assert "memory_context" in p
    assert "only when they change today's call" in p.lower()
    assert "low-confidence" in p
    assert "silently" in p.lower()
    assert "go with the data" in p.lower()


def test_focus_prompt_activates_memory():
    p = ai_client.FOCUS_SYSTEM_PROMPT
    assert "memory_context" in p
    assert "one action item" in p.lower()


def test_weekly_prompt_activates_memory():
    assert "memory_context" in ai_client.WEEKLY_SYSTEM_PROMPT


def test_creepy_guard_present_in_qa_and_morning():
    # "use silently; only surface when it explains the advice" — the anti-creepy rule
    assert "I remember" in ai_client.QA_SYSTEM_PROMPT  # ("don't open with 'I remember'")
    assert "only when it explains" in ai_client.SYSTEM_PROMPT.lower() or \
           "only surface it when it helps" in ai_client.SYSTEM_PROMPT.lower()


# --- Q&A payload conditional inclusion --------------------------------------

def test_qa_payload_includes_memory_context_when_present():
    payload = build_qa_payload("how should I train?", _qa_context(), now={}, structured_memories=_MEM)
    assert payload["memory_context"] == _MEM


def test_qa_payload_omits_memory_context_when_absent():
    payload = build_qa_payload("how should I train?", _qa_context(), now={})
    assert "memory_context" not in payload


# --- morning payload conditional inclusion ----------------------------------

def test_daily_payload_includes_memory_context_when_present():
    payload = build_daily_payload(_user(), _daily_snapshot(), structured_memories=_MEM)
    assert payload["memory_context"] == _MEM


def test_daily_payload_omits_memory_context_when_absent():
    payload = build_daily_payload(_user(), _daily_snapshot())
    assert "memory_context" not in payload


# --- weekly payload conditional inclusion -----------------------------------

def test_weekly_payload_includes_memory_context_when_present():
    payload = build_weekly_payload(_user(), _weekly_snapshot(), structured_memories=_MEM)
    assert payload["memory_context"] == _MEM


def test_weekly_payload_omits_memory_context_when_absent():
    payload = build_weekly_payload(_user(), _weekly_snapshot())
    assert "memory_context" not in payload
