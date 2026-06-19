"""Tests for Recommendation Ledger Phase 3B: extraction + checkpoint evaluation."""
from __future__ import annotations

import asyncio
from datetime import date, timedelta

from app.models.recommendation import RecommendationLedger
from app.models.user import User
from app.services import (
    ai_client,
    recommendation_checkpoint as rc,
    recommendation_extractor as rx,
    recommendation_ledger as rl,
)
from app.services.baseline_engine import DailySnapshot, MetricSummary, QAContext
from app.services.coach_payload_builder import build_daily_payload, build_qa_payload
from tests.conftest import make_user

TODAY = date(2026, 6, 19)


# ---------------------------------------------------------------------------
# Extraction parser (ai_client.extract_recommendations, mocked OpenAI)
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, text):
        self.output_text = text
        self.status = "completed"
        self.usage = None


class _Responses:
    def __init__(self, text):
        self._text = text

    async def create(self, **kwargs):
        return _Resp(self._text)


class _Client:
    def __init__(self, text):
        self.responses = _Responses(text)


def test_extract_recommendations_parses_valid_json(monkeypatch):
    payload = (
        '{"recommendations": [{"should_store": true, "recommendation_type": "training", '
        '"title": "Keep strain under 10", "recommendation_text": "Keep day strain under 10.", '
        '"checkpoint_metric": "strain", "trigger_data": {"strain_limit": 10}}]}'
    )
    monkeypatch.setattr(ai_client, "_client", _Client(payload))
    out = asyncio.run(ai_client.extract_recommendations("Keep strain under 10 today.", "daily", {}, user_id=1))
    assert len(out) == 1 and out[0]["recommendation_type"] == "training"


def test_extract_recommendations_junk_returns_empty(monkeypatch):
    monkeypatch.setattr(ai_client, "_client", _Client("not json"))
    assert asyncio.run(ai_client.extract_recommendations("hi", "qa", {}, user_id=1)) == []


# ---------------------------------------------------------------------------
# validate_and_store (deterministic)
# ---------------------------------------------------------------------------

def _candidate(**over):
    base = {
        "should_store": True,
        "recommendation_type": "training",
        "title": "Keep strain under 10",
        "recommendation_text": "Keep day strain under 10 today.",
        "reason": "HRV 16% below baseline",
        "expected_outcome": "recovery stabilizes tomorrow",
        "checkpoint_metric": "strain",
        "confidence": "high",
        "tags": ["strain"],
        "trigger_data": {"strain_limit": 10},
    }
    base.update(over)
    return base


def test_specific_training_recommendation_stored(mem_session):
    make_user(mem_session)
    summary = rx.validate_and_store(
        mem_session, 1, [_candidate()], source_type="daily", local_date=TODAY
    )
    assert summary["stored"] == 1
    rec = rl.get_pending(mem_session, 1)[0]
    assert rec.recommendation_type == "training"
    assert rec.source_type == "daily"
    assert rec.checkpoint_metric == "strain"
    assert rec.checkpoint_date == TODAY + timedelta(days=1)  # backend sets timing


def test_generic_advice_skipped(mem_session):
    make_user(mem_session)
    generic = _candidate(title="Stay hydrated", recommendation_text="Stay hydrated and rest.",
                         checkpoint_metric=None, trigger_data={})
    summary = rx.validate_and_store(mem_session, 1, [generic], source_type="daily", local_date=TODAY)
    assert summary["stored"] == 0 and summary["skipped"] == 1
    assert rl.get_pending(mem_session, 1) == []


def test_should_store_false_skipped(mem_session):
    make_user(mem_session)
    summary = rx.validate_and_store(
        mem_session, 1, [_candidate(should_store=False)], source_type="daily", local_date=TODAY
    )
    assert summary["skipped"] == 1


def test_empty_title_or_text_skipped(mem_session):
    make_user(mem_session)
    summary = rx.validate_and_store(
        mem_session, 1, [_candidate(title="  "), _candidate(recommendation_text="")],
        source_type="daily", local_date=TODAY,
    )
    assert summary["stored"] == 0 and summary["skipped"] == 2


def test_max_three_per_message(mem_session):
    make_user(mem_session)
    cands = [
        _candidate(title=f"Action {i}", recommendation_text=f"Do action {i}.", recommendation_type="behavior")
        for i in range(5)
    ]
    summary = rx.validate_and_store(mem_session, 1, cands, source_type="daily", local_date=TODAY)
    assert summary["stored"] == 3
    assert mem_session.query(RecommendationLedger).count() == 3


def test_duplicate_merges(mem_session):
    make_user(mem_session)
    rx.validate_and_store(mem_session, 1, [_candidate()], source_type="daily", local_date=TODAY)
    summary = rx.validate_and_store(
        mem_session, 1, [_candidate(title="keep strain under 10.")],  # case/punct differ
        source_type="daily", local_date=TODAY,
    )
    assert summary["merged"] == 1
    assert mem_session.query(RecommendationLedger).count() == 1


def test_weekly_checkpoint_offset_is_seven_days(mem_session):
    make_user(mem_session)
    rx.validate_and_store(
        mem_session, 1,
        [_candidate(recommendation_type="sleep", title="Keep wake time steady this week",
                    recommendation_text="Keep wake time within 45 min this week.",
                    checkpoint_metric="sleep_hours", trigger_data={"target_hours": 8})],
        source_type="weekly", local_date=TODAY,
    )
    rec = rl.get_pending(mem_session, 1)[0]
    assert rec.checkpoint_date == TODAY + timedelta(days=7)


def test_invalid_type_becomes_general_and_unknown_metric_has_no_checkpoint(mem_session):
    make_user(mem_session)
    rx.validate_and_store(
        mem_session, 1,
        [_candidate(recommendation_type="bogus", checkpoint_metric="bogus", trigger_data={})],
        source_type="qa", local_date=TODAY,
    )
    rec = rl.get_pending(mem_session, 1)[0]
    assert rec.recommendation_type == "general"
    assert rec.checkpoint_metric is None
    assert rec.checkpoint_date is None


# ---------------------------------------------------------------------------
# Pure checkpoint evaluators
# ---------------------------------------------------------------------------

def test_evaluate_recovery_outcomes():
    assert rc.evaluate_recovery(42, 61)[0] == "improved"
    assert rc.evaluate_recovery(61, 50)[0] == "worsened"
    assert rc.evaluate_recovery(60, 62)[0] == "neutral"
    assert rc.evaluate_recovery(None, 61)[0] == "inconclusive"
    assert rc.evaluate_recovery(50, None)[0] == "inconclusive"


def test_evaluate_sleep_outcomes():
    assert rc.evaluate_sleep(8.0, 8.2)[0] == "improved"
    assert rc.evaluate_sleep(8.0, 6.5)[0] == "worsened"
    assert rc.evaluate_sleep(8.0, 7.7)[0] == "neutral"
    assert rc.evaluate_sleep(None, 8.0)[0] == "inconclusive"


def test_evaluate_strain_followed():
    assert rc.evaluate_strain(10, 8.7) == ("neutral", "followed",
                                           "Day strain was 8.7, within the suggested limit of 10.")
    assert rc.evaluate_strain(10, 13.0)[1] == "not_followed"
    assert rc.evaluate_strain(None, 8.0)[0] == "inconclusive"


def test_evaluate_weight_conservative():
    assert rc.evaluate_weight(None, 200, 198)[0] == "inconclusive"   # no target
    assert rc.evaluate_weight(190, 200, 197)[0] == "improved"        # toward target
    assert rc.evaluate_weight(190, 197, 200)[0] == "worsened"        # away from target


# ---------------------------------------------------------------------------
# evaluate_due (orchestration, injectable lookup)
# ---------------------------------------------------------------------------

def _lookup(values):
    def lookup(user_id, day, metric):
        return values.get((day, metric))
    return lookup


def test_evaluate_due_marks_recovery_improved(mem_session):
    make_user(mem_session)
    rec = rl.create_recommendation(
        mem_session, 1, source_type="daily", recommendation_type="training",
        title="Easy day", recommendation_text="Easy movement only today.",
        checkpoint_metric="recovery", checkpoint_date=TODAY, local_date=TODAY - timedelta(days=1),
    )
    lookup = _lookup({((TODAY - timedelta(days=1)), "recovery"): 42, (TODAY, "recovery"): 61})
    summary = rc.evaluate_due(mem_session, 1, TODAY, metric_lookup=lookup)
    assert summary == {"due": 1, "checked": 1, "inconclusive": 0}
    refreshed = rl.get_recommendation(mem_session, rec.id)
    assert refreshed.status == "checked"
    assert refreshed.outcome_status == "improved"
    assert refreshed.checked_at is not None


def test_evaluate_due_missing_metric_is_inconclusive(mem_session):
    make_user(mem_session)
    rec = rl.create_recommendation(
        mem_session, 1, source_type="daily", recommendation_type="training",
        title="Easy day", recommendation_text="Easy movement only today.",
        checkpoint_metric="recovery", checkpoint_date=TODAY, local_date=TODAY - timedelta(days=1),
    )
    summary = rc.evaluate_due(mem_session, 1, TODAY, metric_lookup=_lookup({}))
    assert summary["inconclusive"] == 1
    assert rl.get_recommendation(mem_session, rec.id).status == "inconclusive"


def test_evaluate_due_runs_once(mem_session):
    make_user(mem_session)
    rl.create_recommendation(
        mem_session, 1, source_type="daily", recommendation_type="training",
        title="Easy day", recommendation_text="Easy movement only today.",
        checkpoint_metric="recovery", checkpoint_date=TODAY, local_date=TODAY - timedelta(days=1),
    )
    lookup = _lookup({((TODAY - timedelta(days=1)), "recovery"): 42, (TODAY, "recovery"): 61})
    first = rc.evaluate_due(mem_session, 1, TODAY, metric_lookup=lookup)
    second = rc.evaluate_due(mem_session, 1, TODAY, metric_lookup=lookup)
    assert first["checked"] == 1
    assert second == {"due": 0, "checked": 0, "inconclusive": 0}  # already resolved


def test_evaluate_due_skips_future_checkpoints(mem_session):
    make_user(mem_session)
    rl.create_recommendation(
        mem_session, 1, source_type="daily", recommendation_type="training",
        title="Future", recommendation_text="x", checkpoint_metric="recovery",
        checkpoint_date=TODAY + timedelta(days=2), local_date=TODAY,
    )
    assert rc.evaluate_due(mem_session, 1, TODAY, metric_lookup=_lookup({}))["due"] == 0


# ---------------------------------------------------------------------------
# Payload wiring
# ---------------------------------------------------------------------------

_REC_CTX = [{"id": 1, "type": "training", "recommendation": "Easy day", "status": "checked", "outcome": "improved"}]


def _user():
    return User(telegram_id=1, timezone="America/Detroit", first_name="T", goal="general_health")


def _daily_snapshot():
    m = MetricSummary(None, None, None, None)
    return DailySnapshot(
        target_date=TODAY, recovery=m, sleep_hours=m, resting_hr=m, hrv=m,
        yesterday_strain=None, yesterday_workout_count=None, yesterday_workout_minutes=None,
        data_days_available=10, data_maturity="established", safety_triggers=[],
    )


def _qa_context():
    return QAContext(
        data_days_available=10, data_maturity="established", avg_7d={}, avg_30d={},
        recent_tags=[], observations=[], recent_daily_data=[], today_date="2026-06-19",
    )


def test_daily_payload_includes_recommendation_context_when_present():
    p = build_daily_payload(_user(), _daily_snapshot(), recommendation_context=_REC_CTX)
    assert p["recommendation_context"] == _REC_CTX


def test_daily_payload_omits_recommendation_context_when_absent():
    p = build_daily_payload(_user(), _daily_snapshot())
    assert "recommendation_context" not in p


def test_qa_payload_includes_recommendation_context_when_present():
    p = build_qa_payload("how should I train?", _qa_context(), now={}, recommendation_context=_REC_CTX)
    assert p["recommendation_context"] == _REC_CTX


def test_qa_payload_omits_recommendation_context_when_absent():
    p = build_qa_payload("how should I train?", _qa_context(), now={})
    assert "recommendation_context" not in p


# ---------------------------------------------------------------------------
# Prompt activation (cautious closed-loop language)
# ---------------------------------------------------------------------------

def test_morning_prompt_has_cautious_closed_loop_rules():
    p = ai_client.SYSTEM_PROMPT
    assert "recommendation_context" in p
    assert "causation" in p.lower()
    assert "seems consistent" in p.lower()


def test_qa_and_focus_prompts_reference_recommendation_context():
    assert "recommendation_context" in ai_client.QA_SYSTEM_PROMPT
    assert "recommendation_context" in ai_client.FOCUS_SYSTEM_PROMPT
