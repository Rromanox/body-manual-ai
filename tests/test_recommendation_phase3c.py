"""Tests for Recommendation Ledger Phase 3C: idempotency, dedup, control, polish."""
from __future__ import annotations

import inspect
from datetime import date, timedelta

from app.models.recommendation import RecommendationLedger
from app.services import (
    recommendation_checkpoint as rc,
    recommendation_extractor as rx,
    recommendation_ledger as rl,
)
from tests.conftest import make_user

TODAY = date(2026, 6, 19)


def _candidate(**over):
    base = {
        "should_store": True,
        "recommendation_type": "training",
        "title": "Keep strain under 10 today",
        "recommendation_text": "Keep day strain under 10 today.",
        "checkpoint_metric": "strain",
        "confidence": "high",
        "tags": ["strain"],
        "trigger_data": {"strain_limit": 10},
    }
    base.update(over)
    return base


def _lookup(values):
    def lookup(user_id, day, metric):
        return values.get((day, metric))
    return lookup


# --- idempotency ------------------------------------------------------------

def test_source_message_idempotency(mem_session):
    make_user(mem_session)
    rx.validate_and_store(mem_session, 1, [_candidate()], source_type="daily",
                          local_date=TODAY, source_message_id=100)
    assert rl.exists_for_source_message(mem_session, 1, 100) is True
    summary = rx.validate_and_store(mem_session, 1, [_candidate()], source_type="daily",
                                    local_date=TODAY, source_message_id=100)
    assert summary.get("already_processed") is True
    assert mem_session.query(RecommendationLedger).count() == 1


def test_repeated_extraction_without_source_id_dedups(mem_session):
    make_user(mem_session)
    rx.validate_and_store(mem_session, 1, [_candidate()], source_type="focus", local_date=TODAY)
    rx.validate_and_store(mem_session, 1, [_candidate()], source_type="focus", local_date=TODAY)
    assert mem_session.query(RecommendationLedger).count() == 1


# --- dedup signature --------------------------------------------------------

def test_title_drift_dedups(mem_session):
    make_user(mem_session)
    variants = [
        _candidate(title="Keep strain under 10 today", recommendation_text="Keep strain under 10 today."),
        _candidate(title="Stay below 10 strain today", recommendation_text="Stay below 10 strain today."),
        _candidate(title="Limit strain to under 10", recommendation_text="Limit strain to under 10."),
    ]
    summary = rx.validate_and_store(mem_session, 1, variants, source_type="daily", local_date=TODAY)
    assert summary["stored"] == 1 and summary["merged"] == 2
    assert mem_session.query(RecommendationLedger).count() == 1


def test_distinct_advice_not_deduped(mem_session):
    make_user(mem_session)
    cands = [
        _candidate(title="Keep strain under 10", recommendation_text="Keep strain under 10."),
        _candidate(title="Go to bed before 10:45 PM", recommendation_text="Go to bed before 10:45 PM.",
                   recommendation_type="sleep", checkpoint_metric="sleep_hours",
                   trigger_data={"target_bedtime": "10:45"}),
        _candidate(title="Avoid late heavy dinner", recommendation_text="Avoid a late heavy dinner.",
                   recommendation_type="nutrition", checkpoint_metric="recovery", trigger_data={}),
    ]
    summary = rx.validate_and_store(mem_session, 1, cands, source_type="daily", local_date=TODAY)
    assert summary["stored"] == 3


def test_dedup_signature_target_collapses_title_drift():
    a = rl.dedup_signature("training", "strain", {"strain_limit": 10}, "Keep strain under 10", "x")
    b = rl.dedup_signature("training", "strain", {"strain_limit": 10}, "Stay below 10 strain", "y")
    assert a == b


def test_dedup_signature_distinct_when_targets_differ():
    a = rl.dedup_signature("training", "strain", {"strain_limit": 10}, "x", "x")
    b = rl.dedup_signature("sleep", "sleep_hours", {"target_bedtime": "10:45"}, "x", "x")
    assert a != b


def test_dedup_signature_title_fallback_keeps_distinct():
    a = rl.dedup_signature("nutrition", "recovery", {}, "Avoid late dinner", "Avoid late dinner")
    b = rl.dedup_signature("nutrition", "recovery", {}, "Eat more protein", "Eat more protein")
    assert a != b


# --- extraction filters -----------------------------------------------------

def test_generic_advice_skipped(mem_session):
    make_user(mem_session)
    for title in ("Stay hydrated", "Rest and recover", "Listen to your body", "Prioritize sleep"):
        c = _candidate(title=title, recommendation_text=f"{title}.", checkpoint_metric=None, trigger_data={})
        s = rx.validate_and_store(mem_session, 1, [c], source_type="daily", local_date=TODAY)
        assert s["stored"] == 0, title
    assert mem_session.query(RecommendationLedger).count() == 0


def test_question_skipped(mem_session):
    make_user(mem_session)
    c = _candidate(title="Should you rest today?", recommendation_text="Should you rest today?")
    s = rx.validate_and_store(mem_session, 1, [c], source_type="qa", local_date=TODAY)
    assert s["stored"] == 0 and s["skipped"] == 1


def test_explanation_only_skipped(mem_session):
    make_user(mem_session)
    c = _candidate(title="Your recovery is low", recommendation_text="Your recovery is low.",
                   checkpoint_metric=None, trigger_data={})
    s = rx.validate_and_store(mem_session, 1, [c], source_type="daily", local_date=TODAY)
    assert s["stored"] == 0 and s["skipped"] == 1


def test_specific_strain_advice_stored(mem_session):
    make_user(mem_session)
    s = rx.validate_and_store(mem_session, 1, [_candidate()], source_type="daily", local_date=TODAY)
    assert s["stored"] == 1


def test_specific_bedtime_advice_stored(mem_session):
    make_user(mem_session)
    c = _candidate(title="Go to bed before 10:45", recommendation_text="Go to bed before 10:45 PM.",
                   recommendation_type="sleep", checkpoint_metric="sleep_hours",
                   trigger_data={"target_hours": 8})
    s = rx.validate_and_store(mem_session, 1, [c], source_type="daily", local_date=TODAY)
    assert s["stored"] == 1


# --- /recs controls (service layer) -----------------------------------------

def _make_rec(session, **over):
    base = dict(
        source_type="daily", recommendation_type="training",
        title="Keep strain under 10", recommendation_text="Keep strain under 10 today.",
        local_date=TODAY,
    )
    base.update(over)
    return rl.create_recommendation(session, 1, **base)


def test_cancel(mem_session):
    make_user(mem_session)
    rec = _make_rec(mem_session)
    assert rl.cancel(mem_session, rec.id).status == "cancelled"


def test_set_followed_status_variants(mem_session):
    make_user(mem_session)
    for status in ("followed", "not_followed", "partial"):
        rec = _make_rec(mem_session, title=f"rec {status}")
        out = rl.set_followed_status(mem_session, rec.id, status)
        assert out.followed_status == status


def test_set_followed_status_invalid_raises(mem_session):
    make_user(mem_session)
    rec = _make_rec(mem_session)
    try:
        rl.set_followed_status(mem_session, rec.id, "bogus")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_recs_command_wired():
    from app.telegram import handlers
    assert inspect.iscoroutinefunction(handlers.recs_command)
    assert handlers._RECS_FOLLOW_ACTIONS == {
        "followed": "followed", "notfollowed": "not_followed", "partial": "partial",
    }


# --- render output ----------------------------------------------------------

def test_render_pending_shows_checkpoint_detail(mem_session):
    make_user(mem_session)
    rec = _make_rec(mem_session, checkpoint_metric="recovery",
                    checkpoint_date=TODAY + timedelta(days=1), confidence="high")
    text = rl.render_recommendation_list([rec], "Pending recommendations")
    assert "Training — Keep strain under 10" in text
    assert "Checkpoint:" in text and "Metric: recovery" in text
    assert "Source: daily" in text and "Confidence: high" in text


def test_render_checked_shows_outcome(mem_session):
    make_user(mem_session)
    rec = _make_rec(mem_session)
    rl.mark_checked(mem_session, rec.id, outcome_status="improved",
                    outcome_summary="Recovery improved the next day.")
    text = rl.render_recommendation_list([rl.get_recommendation(mem_session, rec.id)], "Checked recently")
    assert "Outcome: improved" in text and "Followed:" in text
    assert "Recovery improved the next day." in text


# --- context cleanup --------------------------------------------------------

def test_cancelled_excluded_from_context(mem_session):
    make_user(mem_session)
    keep = _make_rec(mem_session, title="Keep me")
    drop = _make_rec(mem_session, title="Drop me", recommendation_type="sleep")
    rl.cancel(mem_session, drop.id)
    ctx = rl.build_context(mem_session, 1, TODAY)
    ids = {c["id"] for c in ctx}
    assert keep.id in ids and drop.id not in ids


# --- checkpoint eval respects manual follow-through -------------------------

def test_not_followed_makes_checkpoint_inconclusive(mem_session):
    make_user(mem_session)
    rec = _make_rec(mem_session, title="Easy day", checkpoint_metric="recovery",
                    checkpoint_date=TODAY, local_date=TODAY - timedelta(days=1))
    rl.set_followed_status(mem_session, rec.id, "not_followed")
    # Metrics would normally read as "improved" — but not-followed overrides.
    lookup = _lookup({((TODAY - timedelta(days=1)), "recovery"): 42, (TODAY, "recovery"): 61})
    rc.evaluate_due(mem_session, 1, TODAY, metric_lookup=lookup)
    refreshed = rl.get_recommendation(mem_session, rec.id)
    assert refreshed.status == "inconclusive"
    assert "not followed" in refreshed.outcome_summary.lower()


def test_followed_note_added_to_summary(mem_session):
    make_user(mem_session)
    rec = _make_rec(mem_session, title="Easy day", checkpoint_metric="recovery",
                    checkpoint_date=TODAY, local_date=TODAY - timedelta(days=1))
    rl.set_followed_status(mem_session, rec.id, "followed")
    lookup = _lookup({((TODAY - timedelta(days=1)), "recovery"): 42, (TODAY, "recovery"): 61})
    rc.evaluate_due(mem_session, 1, TODAY, metric_lookup=lookup)
    refreshed = rl.get_recommendation(mem_session, rec.id)
    assert refreshed.outcome_status == "improved"
    assert refreshed.followed_status == "followed"  # not overwritten by inference
    assert "marked this as followed" in refreshed.outcome_summary.lower()
