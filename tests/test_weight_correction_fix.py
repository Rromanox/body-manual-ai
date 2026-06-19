"""Tests for the weight-projection / correction-routing / placeholder bugfix."""
from __future__ import annotations

from datetime import date

from app.services import ai_client, message_intent, output_guard
from app.services import weight_projection as wp
from app.services.baseline_engine import QAContext
from app.services.coach_payload_builder import build_qa_payload

TODAY = date(2026, 6, 19)


# --- deterministic projection (the core repro) ------------------------------

def test_low_rate_is_about_21_weeks_not_2():
    p = wp.project_weight(198.6, 190, -0.4, TODAY)  # -0.4 = losing 0.4/week
    assert p["status"] == "projected"
    assert p["pounds_remaining"] == 8.6
    assert p["estimated_weeks"] == 21.5  # NOT 2
    assert p["rate_lbs_per_week"] == 0.4


def test_fast_rate_is_about_1_6_weeks():
    p = wp.project_weight(198.6, 190, -5.3, TODAY)
    assert p["status"] == "projected"
    assert p["estimated_weeks"] == 1.6


def test_at_or_below_goal_reached():
    assert wp.project_weight(190, 190, -0.4, TODAY)["status"] == "reached"
    assert wp.project_weight(188, 190, -0.4, TODAY)["status"] == "reached"


def test_zero_negative_or_missing_rate_unavailable():
    assert wp.project_weight(198.6, 190, 0.0, TODAY)["status"] == "unavailable"      # stalled
    assert wp.project_weight(198.6, 190, 0.5, TODAY)["status"] == "unavailable"      # gaining (moving away)
    assert wp.project_weight(198.6, 190, None, TODAY)["status"] == "unavailable"     # no rate
    assert wp.project_weight(198.6, 190, 0.5, TODAY)["reason"] == "moving_away"


def test_missing_inputs_return_none():
    assert wp.project_weight(None, 190, -0.4, TODAY) is None
    assert wp.project_weight(198.6, None, -0.4, TODAY) is None


def test_short_term_flag():
    assert wp.project_weight(198.6, 190, -5.3, TODAY, trend_days=5)["short_term"] is True
    assert wp.project_weight(198.6, 190, -0.4, TODAY, trend_days=16)["short_term"] is False


def test_format_projection_has_no_placeholder():
    for rate in (-0.4, -5.3, 0.0, 0.5, None):
        text = wp.format_projection(wp.project_weight(198.6, 190, rate, TODAY))
        assert not output_guard.has_unresolved_placeholder(text), rate
    assert "21.5 weeks" in wp.format_projection(wp.project_weight(198.6, 190, -0.4, TODAY))


# --- correction detection ---------------------------------------------------

def test_corrections_detected():
    for msg in [
        "math ain't mathing bro",
        "If I'm losing 0.4 lbs per week how am I gonna get to 190 in two weeks math aint mathing bro",
        "check that again",
        "Your 0.4 lbs per week loss is wrong check that again",
        "wym",
        "wym got it",
        "recalculate",
        "that doesn't make sense",
        "your number is wrong",
    ]:
        assert message_intent.is_correction(msg) is True, msg


def test_non_corrections_not_flagged():
    for msg in ["what was my hrv yesterday", "had pizza at 9pm", "I took reta today", "thanks"]:
        assert message_intent.is_correction(msg) is False, msg


# --- status-memory classification -------------------------------------------

def test_status_memory_detected():
    assert message_intent.detect_status_memory("remember I'm taking retatrutide") == "retatrutide"
    assert message_intent.detect_status_memory("Dont worry about it remember I'm taking retatrutide") == "retatrutide"
    assert message_intent.detect_status_memory("I'm on retatrutide") == "retatrutide"
    assert message_intent.detect_status_memory("I'm taking creatine") == "creatine"


def test_status_memory_ignores_non_status():
    assert message_intent.detect_status_memory("I'm taking longer to recover") is None
    assert message_intent.detect_status_memory("I'm taking a break") is None
    assert message_intent.detect_status_memory("what is retatrutide?") is None
    assert message_intent.detect_status_memory("I will take my retatrutide Friday") is None  # future


def test_reta_log_vs_status_classification():
    from app.services import health_reminder as hr
    # "took reta today" -> reta log, not status
    assert hr.detect_reta_message("I took reta today", TODAY) == {"action": "log", "date": TODAY}
    assert message_intent.detect_status_memory("I took reta today") is None
    # "remember I'm taking retatrutide" -> status, not a reta log
    assert hr.detect_reta_message("remember I'm taking retatrutide", TODAY) is None
    assert message_intent.detect_status_memory("remember I'm taking retatrutide") == "retatrutide"


# --- placeholder guard ------------------------------------------------------

def test_placeholder_detected():
    for bad in [
        "you'll reach 190 lbs in about time.",
        "around {date}",
        "due on <date>",
        "that's N/A weeks away",
        "timeline: TBD",
        "estimated: None",
    ]:
        assert output_guard.has_unresolved_placeholder(bad) is True, bad


def test_clean_text_not_flagged():
    for ok in [
        "you'll hit 190 in about 2 weeks, around July 3",
        "Recovery improved from 42 to 61.",
        "none of your metrics look off",  # lowercase 'none' is fine
    ]:
        assert output_guard.has_unresolved_placeholder(ok) is False, ok


# --- payload + prompt wiring ------------------------------------------------

def _qa_ctx(**over):
    base = dict(
        data_days_available=40, data_maturity="established", avg_7d={}, avg_30d={},
        recent_tags=[], observations=[], recent_daily_data=[], today_date="2026-06-19",
    )
    base.update(over)
    return QAContext(**base)


def test_qa_payload_includes_weight_projection():
    proj = wp.project_weight(198.6, 190, -0.4, TODAY)
    payload = build_qa_payload("when will I hit 190?", _qa_ctx(weight_projection=proj), now={})
    assert payload["weight_projection"]["estimated_weeks"] == 21.5


def test_qa_payload_omits_projection_when_absent():
    assert "weight_projection" not in build_qa_payload("hi", _qa_ctx(), now={})


def test_prompts_carry_new_rules():
    assert "weight_projection" in ai_client.QA_SYSTEM_PROMPT
    assert "Corrections" in ai_client.QA_SYSTEM_PROMPT
    assert "in about time" in ai_client.QA_SYSTEM_PROMPT  # placeholder ban example
    # event extractor no longer treats present-tense status as a commitment
    assert "not a commitment" in ai_client.EVENT_EXTRACTOR_SYSTEM_PROMPT
