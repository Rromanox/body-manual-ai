"""Accuracy Guard 2: date validation, stall hypotheticals, constraint memory."""
from __future__ import annotations

from datetime import date, timedelta

from app.services import ai_client, memory_retriever, memory_store, message_intent, output_guard
from app.services import weight_projection as wp
from tests.conftest import make_user

JUN19 = date(2026, 6, 19)


# --- Part 1: backend date + validation --------------------------------------

def test_projection_has_backend_date_and_days():
    p = wp.project_weight(198.6, 190, -5.3, JUN19)
    assert p["estimated_weeks"] == 1.6
    assert p["estimated_days"] == 11           # 8.6/5.3*7 = 11.36 -> 11
    assert p["estimated_date"] == "2026-06-30"  # Jun 19 + 11 days


def test_low_rate_keeps_correct_far_date():
    p = wp.project_weight(198.6, 190, -0.4, JUN19)
    assert p["estimated_weeks"] == 21.5
    assert p["estimated_date"] == str(JUN19 + timedelta(days=p["estimated_days"]))


def test_date_validation_rejects_wrong_dates():
    p = wp.project_weight(198.6, 190, -5.3, JUN19)  # est 2026-06-30
    assert output_guard.projection_date_is_consistent("around June 26", p) is False
    assert output_guard.projection_date_is_consistent("around July 3", p) is False  # 3 days off
    assert output_guard.projection_date_is_consistent("reach 190 around June 30", p) is True
    assert output_guard.projection_date_is_consistent("around July 1", p) is True
    # today mentioned alongside the correct date is still fine
    assert output_guard.projection_date_is_consistent("today is June 19, around June 30", p) is True
    # stating weeks but no date is fine
    assert output_guard.projection_date_is_consistent("about 1.6 weeks away", p) is True


def test_date_validation_noop_when_not_projected():
    assert output_guard.projection_date_is_consistent("around July 3", None) is True
    stalled = wp.stall_projection(198.6, 185)
    assert output_guard.projection_date_is_consistent("around July 3", stalled) is True


# --- Part 2: stall + explicit-rate hypotheticals ----------------------------

def test_detect_hypotheticals():
    assert wp.detect_hypothetical("if my weight stalls, when do I hit 185?") == {"type": "stall"}
    assert wp.detect_hypothetical("if I stop losing weight when do I hit 185") == {"type": "stall"}
    assert wp.detect_hypothetical("if my weight plateaus") == {"type": "stall"}
    assert wp.detect_hypothetical("what if I lose 1 lb per week") == {"type": "rate", "rate": 1.0}
    assert wp.detect_hypothetical("what if I lose 2 lbs per week") == {"type": "rate", "rate": 2.0}
    assert wp.detect_hypothetical("when will I hit 190") is None


def test_stall_question_has_no_projected_date():
    p = wp.projection_for_question("if my weight stalls, when do I hit 185?", 198.6, 190, -5.3, JUN19)
    assert p["status"] == "unavailable" and p["reason"] == "stall_hypothetical"
    assert p["goal_lbs"] == 185
    assert "no projected date" in wp.format_projection(p).lower()


def test_explicit_rate_hypothetical_uses_that_rate():
    p = wp.projection_for_question("what if I lose 1 lb per week", 198.6, 190, -5.3, JUN19)
    assert p["status"] == "projected" and p["rate_lbs_per_week"] == 1.0
    assert p["estimated_weeks"] == 8.6  # 8.6 / 1.0


def test_target_weight_parsed_from_question():
    p = wp.projection_for_question("when do I hit 185?", 198.6, 190, -5.3, JUN19)
    assert p["goal_lbs"] == 185
    assert p["pounds_remaining"] == 13.6


# --- Part 3: constraint / preference memory ---------------------------------

def test_constraint_detected():
    c = message_intent.detect_constraint_memory("I can only train mornings before 7am")
    assert c == {"type": "constraint", "content": "Can only train mornings before 7am"}
    assert message_intent.detect_constraint_memory("I can't train at night")["type"] == "constraint"
    assert message_intent.detect_constraint_memory("I only have 30 minutes")["type"] == "constraint"
    assert message_intent.detect_constraint_memory("I don't have access to a gym")["type"] == "constraint"
    assert message_intent.detect_constraint_memory("I work 7:30am to 10:30pm")["type"] == "constraint"


def test_preference_detected():
    assert message_intent.detect_constraint_memory("I usually train in the morning")["type"] == "preference"
    assert message_intent.detect_constraint_memory("I prefer short workouts")["type"] == "preference"


def test_constraint_ignores_questions_and_followthrough():
    assert message_intent.detect_constraint_memory("Can I train tomorrow morning?") is None
    assert message_intent.detect_constraint_memory("I ended up at 13 strain") is None
    assert message_intent.detect_constraint_memory("No, I didn't train today") is None


# --- Part 4: constraint surfaces in Q&A retrieval ---------------------------

def test_constraint_memory_retrieved_for_training_plan(mem_session):
    make_user(mem_session)
    memory_store.add_memory(
        mem_session, 1, "constraint", "Can only train mornings before 7am",
        source="user_stated", confidence="high", tags=["training", "schedule"],
    )
    out = memory_retriever.for_qa(mem_session, 1, "what's a good training plan for me this week?")
    assert any("mornings before 7am" in m["content"] for m in out)


def test_training_plan_prompt_rules_present():
    p = ai_client.QA_SYSTEM_PROMPT
    assert "Training plans" in p
    assert "before 7am" in p
    assert "never suggest an evening workout" in p
    assert "red/yellow/green" in p
    # date rule: AI must use the backend's estimated_date, and handle stalls
    assert "estimated_date" in p
    assert "stall_hypothetical" in p


# --- Part 5: regression (other detectors unaffected) ------------------------

def test_status_and_correction_still_work():
    assert message_intent.detect_status_memory("remember I'm taking retatrutide") == "retatrutide"
    assert message_intent.is_correction("math ain't mathing") is True
    # a constraint isn't a correction or a status
    assert message_intent.is_correction("I can only train mornings before 7am") is False
    assert message_intent.detect_status_memory("I can only train mornings before 7am") is None
