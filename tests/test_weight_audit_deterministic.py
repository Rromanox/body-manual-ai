"""Deterministic weight-audit replies (no AI) + strengthened date-weight parser."""
from __future__ import annotations

from datetime import date

from app.services import output_guard
from app.services import weight_trends as wt
from app.services import weight_projection as wp

JUN19 = date(2026, 6, 19)
_WEIGHTS = [
    (date(2026, 6, 14), 202.4),
    (date(2026, 6, 15), 200.0),
    (date(2026, 6, 16), 199.9),
    (date(2026, 6, 17), 200.4),
    (date(2026, 6, 18), 200.4),
    (date(2026, 6, 19), 198.6),
]
_AUDIT = wt.build_weight_trend_audit(_WEIGHTS, JUN19)

# words that would indicate generic coaching advice leaked into a data answer
_ADVICE_WORDS = ("hydrat", "recovery", "training", "retatrutide", "sleep", "stress", "nutrition", "consult")


# --- deterministic formatter ------------------------------------------------

def test_answer_states_selected_window_method_rate():
    out = wt.format_weight_audit_answer(_AUDIT)
    sel = _AUDIT["selected"]
    assert f"last {sel['window_days']} days" in out
    assert sel["method"].replace("_", " ") in out
    assert str(abs(sel["rate_lbs_per_week"])) in out


def test_answer_lists_only_actual_rows_correctly_dated():
    out = wt.format_weight_audit_answer(_AUDIT)
    assert "Jun 17: 200.4 lb" in out          # correct
    assert "Jun 17: 202.4 lb" not in out       # the transcript's mistake
    assert "Jun 14: 202.4 lb" in out           # 202.4 belongs to Jun 14


def test_answer_has_no_generic_advice():
    out = wt.format_weight_audit_answer(_AUDIT, projection=wp.project_weight(198.6, 190, -3.4, JUN19)).lower()
    for w in _ADVICE_WORDS:
        assert w not in out, w


def test_answer_start_weights_are_real_readings_not_averages():
    out = wt.format_weight_audit_answer(_AUDIT)
    known = set(_AUDIT["known_weights"].values())
    # every window's start weight printed must be an actual stored reading
    for label, w in _AUDIT["windows"].items():
        assert w["start_weight"] in known
    # explicit anti-average wording present
    assert "not a simple 7-day average" in out.lower() or "average" in out.lower()


def test_answer_includes_projection_when_provided():
    proj = wp.project_weight(198.6, 190, -3.4, JUN19)
    out = wt.format_weight_audit_answer(_AUDIT, projection=proj)
    assert proj["estimated_date"] in out


def test_answer_safe_when_no_audit():
    assert "enough weight" in wt.format_weight_audit_answer(None).lower()


# --- strengthened date-weight parser ----------------------------------------

def test_parser_catches_year_format():
    assert output_guard.weight_data_is_consistent("June 17, 2026: 202.4 lbs", _AUDIT) is False


def test_parser_catches_bold_markdown():
    assert output_guard.weight_data_is_consistent("**June 17**: **202.4 lbs**", _AUDIT) is False


def test_parser_catches_dash_and_abbrev():
    assert output_guard.weight_data_is_consistent("Jun 17, 2026 - 202.4 lb", _AUDIT) is False


def test_parser_accepts_correct_pairs():
    assert output_guard.weight_data_is_consistent("June 17, 2026: 200.4 lbs", _AUDIT) is True
    assert output_guard.weight_data_is_consistent("**Jun 14: 202.4 lb**", _AUDIT) is True


def test_parser_ignores_unknown_dates():
    # a date we don't have a reading for shouldn't trip the guard
    assert output_guard.weight_data_is_consistent("Jan 1: 210.0 lbs", _AUDIT) is True
