"""Tests for Recommendation Ledger Phase 3D: NL follow-through + WHOOP inference."""
from __future__ import annotations

from datetime import date, timedelta

from app.services import (
    recommendation_checkpoint as rc,
    recommendation_extractor as rx,
    recommendation_followthrough as rf,
    recommendation_ledger as rl,
)
from tests.conftest import make_user

TODAY = date(2026, 6, 19)


def _rec(session, *, title, rec_type="training", trigger=None, metric=None,
         checkpoint_date=None, local_date=TODAY):
    return rl.create_recommendation(
        session, 1, source_type="daily", recommendation_type=rec_type,
        title=title, recommendation_text=f"{title}.",
        checkpoint_metric=metric, checkpoint_date=checkpoint_date,
        trigger_data=trigger or {}, local_date=local_date,
    )


def _lookup(values):
    def lookup(user_id, day, metric):
        return values.get((day, metric))
    return lookup


# --- looks_like_followthrough -----------------------------------------------

def test_signal_gate():
    assert rf.looks_like_followthrough("I skipped training") is True
    assert rf.looks_like_followthrough("stayed under 10") is True
    assert rf.looks_like_followthrough("should I train today?") is False  # question
    assert rf.looks_like_followthrough("had pizza at 9pm") is False        # plain log
    assert rf.looks_like_followthrough("") is False


# --- deterministic NL matching ----------------------------------------------

def test_didnt_train_marks_skip_followed(mem_session):
    make_user(mem_session)
    skip = _rec(mem_session, title="Skip training today", trigger={"avoid_workout": True})
    d = rf.match_deterministic("No, I didn't train today.", [skip])
    assert d["should_update"] and d["recommendation_id"] == skip.id
    assert d["followed_status"] == "followed"


def test_trained_anyway_marks_skip_not_followed(mem_session):
    make_user(mem_session)
    skip = _rec(mem_session, title="Skip training today", trigger={"avoid_workout": True})
    d = rf.match_deterministic("I trained anyway", [skip])
    assert d["followed_status"] == "not_followed" and d["recommendation_id"] == skip.id


def test_stayed_under_marks_strain_followed(mem_session):
    make_user(mem_session)
    strain = _rec(mem_session, title="Keep strain under 10", trigger={"strain_limit": 10}, metric="strain")
    d = rf.match_deterministic("I stayed under 10 strain", [strain])
    assert d["followed_status"] == "followed" and d["recommendation_id"] == strain.id


def test_went_over_marks_strain_not_followed(mem_session):
    make_user(mem_session)
    strain = _rec(mem_session, title="Keep strain under 10", trigger={"strain_limit": 10}, metric="strain")
    d = rf.match_deterministic("I went over 10 strain today", [strain])
    assert d["followed_status"] == "not_followed"


def test_ambiguous_two_training_recs_returns_none(mem_session):
    make_user(mem_session)
    a = _rec(mem_session, title="Skip training", trigger={"avoid_workout": True})
    b = _rec(mem_session, title="Easy day", trigger={"easy_day": True})
    assert rf.match_deterministic("I trained anyway", [a, b]) is None


def test_no_pending_returns_none():
    assert rf.match_deterministic("I trained anyway", []) is None


def test_only_walked_defers_to_ai(mem_session):
    make_user(mem_session)
    easy = _rec(mem_session, title="Easy day", trigger={"easy_day": True})
    # nuanced (followed vs partial) -> deterministic declines
    assert rf.match_deterministic("I only walked today", [easy]) is None


# --- apply_decision ---------------------------------------------------------

def test_apply_decision_sets_status(mem_session):
    make_user(mem_session)
    rec = _rec(mem_session, title="Skip training", trigger={"avoid_workout": True})
    out = rf.apply_decision(mem_session, 1, {"should_update": True, "recommendation_id": rec.id, "followed_status": "followed"})
    assert out.followed_status == "followed"


def test_apply_decision_owner_scoped(mem_session):
    make_user(mem_session, user_id=1)
    make_user(mem_session, user_id=2)
    rec = _rec(mem_session, title="Skip training")
    assert rf.apply_decision(mem_session, 2, {"should_update": True, "recommendation_id": rec.id, "followed_status": "followed"}) is None


def test_apply_decision_noop_when_should_not_update(mem_session):
    make_user(mem_session)
    rec = _rec(mem_session, title="Skip training")
    assert rf.apply_decision(mem_session, 1, {"should_update": False}) is None


# --- trigger_data normalization ---------------------------------------------

def test_normalize_parses_strain_limit():
    td = rx._normalize_trigger_data({}, "Keep strain under 10", "Keep strain under 10 today.", "training")
    assert td["strain_limit"] == 10


def test_normalize_parses_target_hours():
    td = rx._normalize_trigger_data({}, "Aim for 8 hours of sleep", "Aim for 8 hours of sleep.", "sleep")
    assert td["target_hours"] == 8


def test_normalize_sets_flags():
    assert rx._normalize_trigger_data({}, "Take it easy today", "Easy movement only.", "training").get("easy_day") is True
    assert rx._normalize_trigger_data({}, "Skip training today", "Skip the workout.", "training").get("avoid_workout") is True
    assert rx._normalize_trigger_data({}, "Avoid a late heavy dinner", "No late dinner.", "nutrition").get("avoid_late_meal") is True


def test_normalize_model_value_wins():
    td = rx._normalize_trigger_data({"strain_limit": 12}, "Keep strain under 10", "x", "training")
    assert td["strain_limit"] == 12


# --- WHOOP inference (pure) -------------------------------------------------

def test_infer_skip_no_workout_low_strain_followed(mem_session):
    make_user(mem_session)
    rec = _rec(mem_session, title="Skip", trigger={"avoid_workout": True})
    lookup = _lookup({(TODAY, "workout_count"): 0, (TODAY, "strain"): 5.0})
    status, note = rc.infer_followthrough(rec, lookup)
    assert status == "followed" and "no workout" in note.lower()


def test_infer_skip_with_workout_not_followed(mem_session):
    make_user(mem_session)
    rec = _rec(mem_session, title="Skip", trigger={"avoid_workout": True})
    lookup = _lookup({(TODAY, "workout_count"): 1, (TODAY, "strain"): 12.0})
    status, _ = rc.infer_followthrough(rec, lookup)
    assert status == "not_followed"


def test_infer_skip_no_workout_high_strain_partial(mem_session):
    make_user(mem_session)
    rec = _rec(mem_session, title="Easy", trigger={"easy_day": True})
    lookup = _lookup({(TODAY, "workout_count"): 0, (TODAY, "strain"): 16.0})
    status, _ = rc.infer_followthrough(rec, lookup)
    assert status == "partial"


def test_infer_strain_limit_below_and_above(mem_session):
    make_user(mem_session)
    rec = _rec(mem_session, title="Strain<10", trigger={"strain_limit": 10})
    assert rc.infer_followthrough(rec, _lookup({(TODAY, "strain"): 8.0}))[0] == "followed"
    assert rc.infer_followthrough(rec, _lookup({(TODAY, "strain"): 13.0}))[0] == "not_followed"


def test_infer_sleep_target_met_missed(mem_session):
    make_user(mem_session)
    rec = _rec(mem_session, title="8h sleep", trigger={"target_hours": 8},
               metric="sleep_hours", checkpoint_date=TODAY + timedelta(days=1))
    cd = TODAY + timedelta(days=1)
    assert rc.infer_followthrough(rec, _lookup({(cd, "sleep_hours"): 8.3}))[0] == "followed"
    assert rc.infer_followthrough(rec, _lookup({(cd, "sleep_hours"): 6.0}))[0] == "not_followed"


def test_infer_missing_data_returns_none(mem_session):
    make_user(mem_session)
    rec = _rec(mem_session, title="Skip", trigger={"avoid_workout": True})
    assert rc.infer_followthrough(rec, _lookup({})) == (None, None)


def test_infer_nutrition_returns_none(mem_session):
    make_user(mem_session)
    rec = _rec(mem_session, title="No late dinner", rec_type="nutrition", trigger={"avoid_late_meal": True})
    assert rc.infer_followthrough(rec, _lookup({(TODAY, "strain"): 5.0})) == (None, None)


# --- evaluate_due uses inferred follow-through ------------------------------

def test_evaluate_due_inferred_not_followed_is_inconclusive(mem_session):
    make_user(mem_session)
    rec = _rec(mem_session, title="Easy day", trigger={"easy_day": True}, metric="recovery",
               checkpoint_date=TODAY, local_date=TODAY - timedelta(days=1))
    yday = TODAY - timedelta(days=1)
    # Worked out yesterday -> inferred not_followed; recovery would look "improved" but we don't claim it.
    lookup = _lookup({(yday, "workout_count"): 1, (yday, "recovery"): 42, (TODAY, "recovery"): 61})
    rc.evaluate_due(mem_session, 1, TODAY, metric_lookup=lookup)
    refreshed = rl.get_recommendation(mem_session, rec.id)
    assert refreshed.status == "inconclusive"
    assert refreshed.followed_status == "not_followed"
    assert "won't treat" in refreshed.outcome_summary.lower()


def test_evaluate_due_inferred_followed_uses_cautious_language(mem_session):
    make_user(mem_session)
    rec = _rec(mem_session, title="Easy day", trigger={"easy_day": True}, metric="recovery",
               checkpoint_date=TODAY, local_date=TODAY - timedelta(days=1))
    yday = TODAY - timedelta(days=1)
    lookup = _lookup({(yday, "workout_count"): 0, (yday, "strain"): 5.0,
                      (yday, "recovery"): 42, (TODAY, "recovery"): 61})
    rc.evaluate_due(mem_session, 1, TODAY, metric_lookup=lookup)
    refreshed = rl.get_recommendation(mem_session, rec.id)
    assert refreshed.status == "checked"
    assert refreshed.outcome_status == "improved"
    assert refreshed.followed_status == "followed"
    summary = refreshed.outcome_summary.lower()
    assert "whoop shows" in summary and "improved" in summary
