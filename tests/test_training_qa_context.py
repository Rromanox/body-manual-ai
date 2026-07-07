"""The coach's Q&A must answer from the REAL seeded plan, not invent one."""
from __future__ import annotations

from datetime import date

from app.services import training_plan as tp
from app.services.baseline_engine import QAContext
from app.services.coach_payload_builder import build_qa_payload
from scripts.seed_training_plan import seed
from tests.conftest import make_user

BEFORE_START = date(2026, 7, 6)  # a few days before the plan begins


def test_qa_training_context_none_when_unseeded(mem_session):
    make_user(mem_session)
    assert tp.qa_training_context(mem_session, 1, BEFORE_START) is None


def test_qa_training_context_reflects_real_plan(mem_session):
    make_user(mem_session)
    seed(mem_session, 1)
    ctx = tp.qa_training_context(mem_session, 1, BEFORE_START)
    assert ctx is not None
    assert ctx["window"] == {"start": "2026-07-13", "end": "2026-10-04", "trip_start": "2026-10-04"}
    # First upcoming session is the real first workout, not a generic one.
    assert ctx["upcoming"][0]["date"] == "2026-07-14"
    assert ctx["upcoming"][0]["type"] == "intervals"
    assert ctx["overview"]["critical_total"] == 3
    assert ctx["critical_ride_dates"] == ["2026-09-05", "2026-09-12", "2026-09-19"]


def _min_ctx(**kw) -> QAContext:
    base = dict(
        data_days_available=0, data_maturity="building_baseline",
        avg_7d={}, avg_30d={}, recent_tags=[], observations=[],
        recent_daily_data=[], today_date="2026-07-06",
    )
    base.update(kw)
    return QAContext(**base)


def test_payload_includes_training_plan_when_present():
    payload = build_qa_payload("what's my training plan?", _min_ctx(training_plan={"overview": {"x": 1}}))
    assert payload["training_plan"] == {"overview": {"x": 1}}


def test_payload_omits_training_plan_when_absent():
    payload = build_qa_payload("how did I sleep?", _min_ctx(training_plan=None))
    assert "training_plan" not in payload
