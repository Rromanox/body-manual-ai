"""Weight Trend Audit Guard: deterministic trend rows + data hallucination guard."""
from __future__ import annotations

from datetime import date

from app.services import ai_client, message_intent, output_guard
from app.services import weight_trends as wt
from app.services.baseline_engine import QAContext
from app.services.coach_payload_builder import build_qa_payload

JUN19 = date(2026, 6, 19)

# The real data from the transcript: Jun 17 = 200.4, Jun 14 = 202.4 (NOT Jun 17 = 202.4).
_WEIGHTS = [
    (date(2026, 6, 14), 202.4),
    (date(2026, 6, 15), 200.0),
    (date(2026, 6, 16), 199.9),
    (date(2026, 6, 17), 200.4),
    (date(2026, 6, 18), 200.4),
    (date(2026, 6, 19), 198.6),
]


def test_audit_preserves_exact_rows():
    audit = wt.build_weight_trend_audit(_WEIGHTS, JUN19)
    assert audit["known_weights"]["2026-06-17"] == 200.4   # not 202.4
    assert audit["known_weights"]["2026-06-14"] == 202.4
    assert audit["current_weight"] == 198.6
    assert audit["current_date"] == "2026-06-19"


def test_trend_start_is_a_real_reading_never_an_average():
    audit = wt.build_weight_trend_audit(_WEIGHTS, JUN19)
    for label, w in audit["windows"].items():
        # start/end weights must be actual stored readings
        assert w["start_weight"] in _dict_vals()
        assert w["end_weight"] in _dict_vals()
        # the start weight must equal the reading on start_date (not an average)
        assert audit["known_weights"][w["start_date"]] == w["start_weight"]


def _dict_vals():
    return {round(w, 1) for _, w in _WEIGHTS}


def test_windows_present_and_distinguishable():
    audit = wt.build_weight_trend_audit(_WEIGHTS, JUN19)
    win = audit["windows"]
    assert "3d" in win and "7d" in win
    # 3-day endpoint pace differs from the 7-day endpoint pace here
    assert win["3d"]["lbs_per_week"] != win["7d"]["lbs_per_week"]
    # 7-day window starts at the earliest reading in range (Jun 14 = 202.4)
    assert win["7d"]["start_date"] == "2026-06-14" and win["7d"]["start_weight"] == 202.4
    assert win["7d"]["end_weight"] == 198.6


def test_selected_rate_names_window_and_method():
    audit = wt.build_weight_trend_audit(_WEIGHTS, JUN19)
    sel = audit["selected"]
    assert sel is not None
    assert sel["method"] in ("linear_regression", "endpoint_change")
    assert sel["window_days"] in (3, 7, 14, 30)
    assert sel["rate_lbs_per_week"] < 0  # losing weight (signed)


def test_audit_none_when_too_sparse():
    assert wt.build_weight_trend_audit([(JUN19, 198.6)], JUN19) is None


# --- output guard: weight date/value hallucination --------------------------

def test_wrong_date_weight_pair_rejected():
    audit = wt.build_weight_trend_audit(_WEIGHTS, JUN19)
    assert output_guard.weight_data_is_consistent("June 17: 202.4 lbs", audit) is False  # really 200.4
    assert output_guard.weight_data_is_consistent("June 17: 200.4 lbs", audit) is True   # correct
    assert output_guard.weight_data_is_consistent("June 14 was 202.4 lbs", audit) is True
    assert output_guard.weight_data_is_consistent("no weights mentioned here", audit) is True
    assert output_guard.weight_data_is_consistent("June 17: 202.4 lbs", None) is True     # no audit


# --- data-audit intent detection --------------------------------------------

def test_data_audit_intents():
    for msg in [
        "is that my average weight loss rate?",
        "check the math and data",
        "where did that rate come from?",
        "show me the data",
        "what weights did you use?",
        "how did you calculate that",
        "what is my 7-day weight trend?",
        "what is my 14 day weight trend",
        "what is my 30-day weight trend?",
    ]:
        assert message_intent.is_data_audit(msg) is True, msg


def test_non_audit_not_flagged():
    for msg in ["how did I sleep?", "had pizza at 9pm", "I took reta today"]:
        assert message_intent.is_data_audit(msg) is False, msg


# --- payload wiring ---------------------------------------------------------

def _ctx(**over):
    base = dict(
        data_days_available=30, data_maturity="established", avg_7d={}, avg_30d={},
        recent_tags=[], observations=[], recent_daily_data=[], today_date="2026-06-19",
    )
    base.update(over)
    return QAContext(**base)


def test_payload_includes_audit_and_projection_rate_source():
    audit = wt.build_weight_trend_audit(_WEIGHTS, JUN19)
    sel = audit["selected"]
    ctx = _ctx(
        weight_trend_audit=audit, weight_current_lbs=198.6,
        weight_weekly_rate_lbs=sel["rate_lbs_per_week"], goal_weight_lbs=190,
    )
    payload = build_qa_payload("when will I hit 190?", ctx, now={"date": "2026-06-19"})
    assert payload["weight_trend_audit"]["known_weights"]["2026-06-17"] == 200.4
    proj = payload["weight_projection"]
    assert proj["status"] == "projected"
    assert proj["selected_rate_window_days"] == sel["window_days"]
    assert proj["selected_rate_method"] == sel["method"]


def test_prompt_has_audit_rules():
    p = ai_client.QA_SYSTEM_PROMPT
    assert "weight_trend_audit" in p
    assert "never use an average" in p.lower() or "never use an average as" in p.lower()
    assert "known_weights" in p
