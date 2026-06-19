"""Tests for the Recommendation Ledger service (Phase 3A, deterministic only)."""
from __future__ import annotations

import importlib.util
import pathlib
from datetime import date, timedelta

import pytest

from app.models.recommendation import RecommendationLedger
from app.services import recommendation_ledger as rl
from tests.conftest import make_user

TODAY = date(2026, 6, 19)


def _create(session, user_id=1, **over):
    base = dict(
        source_type="daily",
        recommendation_type="training",
        title="Keep strain under 10 today",
        recommendation_text="Keep day strain under 10; recovery is low.",
        local_date=TODAY,
    )
    base.update(over)
    return rl.create_recommendation(session, user_id, **base)


# --- create + validation ----------------------------------------------------

def test_create_recommendation(mem_session):
    make_user(mem_session)
    rec = _create(mem_session)
    assert rec.id is not None
    assert rec.status == "pending"
    assert rec.followed_status == "unknown"
    assert rec.outcome_status == "unknown"
    assert rec.confidence == "medium"


def test_create_rejects_unknown_source_type(mem_session):
    make_user(mem_session)
    with pytest.raises(ValueError):
        _create(mem_session, source_type="bogus")


def test_create_rejects_unknown_rec_type(mem_session):
    make_user(mem_session)
    with pytest.raises(ValueError):
        _create(mem_session, recommendation_type="bogus")


def test_create_rejects_empty_title_and_text(mem_session):
    make_user(mem_session)
    with pytest.raises(ValueError):
        _create(mem_session, title="   ")
    with pytest.raises(ValueError):
        _create(mem_session, recommendation_text="")


def test_create_rejects_bad_confidence(mem_session):
    make_user(mem_session)
    with pytest.raises(ValueError):
        _create(mem_session, confidence="banana")


# --- JSON columns on SQLite -------------------------------------------------

def test_json_trigger_data_and_tags_round_trip(mem_session):
    make_user(mem_session)
    rec = _create(
        mem_session,
        trigger_data={"hrv_pct_below": 16, "rhr_delta": 5},
        tags=["strain", "hrv"],
    )
    fetched = rl.get_recommendation(mem_session, rec.id)
    assert fetched.trigger_data == {"hrv_pct_below": 16, "rhr_delta": 5}
    assert fetched.tags == ["strain", "hrv"]


# --- pending / due checkpoints ----------------------------------------------

def test_get_pending_excludes_resolved(mem_session):
    make_user(mem_session)
    keep = _create(mem_session, title="pending one")
    done = _create(mem_session, title="done one", recommendation_type="sleep")
    rl.mark_checked(mem_session, done.id, outcome_status="improved")
    pending = rl.get_pending(mem_session, 1)
    assert [r.id for r in pending] == [keep.id]


def test_get_due_checkpoints(mem_session):
    make_user(mem_session)
    due = _create(mem_session, title="due", checkpoint_metric="recovery_score",
                  checkpoint_date=TODAY - timedelta(days=1))
    today_due = _create(mem_session, title="today", recommendation_type="sleep",
                        checkpoint_metric="recovery_score", checkpoint_date=TODAY)
    _create(mem_session, title="future", recommendation_type="recovery",
            checkpoint_metric="recovery_score", checkpoint_date=TODAY + timedelta(days=2))
    _create(mem_session, title="no checkpoint", recommendation_type="weight")
    out = rl.get_due_checkpoints(mem_session, 1, TODAY)
    ids = {r.id for r in out}
    assert ids == {due.id, today_due.id}  # past + today, not future, not checkpoint-less


def test_due_checkpoints_excludes_non_pending(mem_session):
    make_user(mem_session)
    r = _create(mem_session, checkpoint_metric="recovery_score", checkpoint_date=TODAY)
    rl.cancel(mem_session, r.id)
    assert rl.get_due_checkpoints(mem_session, 1, TODAY) == []


# --- resolution transitions -------------------------------------------------

def test_mark_checked_sets_outcome_and_followed(mem_session):
    make_user(mem_session)
    r = _create(mem_session)
    out = rl.mark_checked(mem_session, r.id, outcome_status="improved",
                          outcome_summary="recovery 42 -> 61", followed_status="followed")
    assert out.status == "checked"
    assert out.outcome_status == "improved"
    assert out.followed_status == "followed"
    assert out.outcome_summary == "recovery 42 -> 61"
    assert out.checked_at is not None


def test_mark_checked_rejects_bad_outcome(mem_session):
    make_user(mem_session)
    r = _create(mem_session)
    with pytest.raises(ValueError):
        rl.mark_checked(mem_session, r.id, outcome_status="exploded")


def test_mark_inconclusive(mem_session):
    make_user(mem_session)
    r = _create(mem_session)
    out = rl.mark_inconclusive(mem_session, r.id, outcome_summary="no recovery data")
    assert out.status == "inconclusive"
    assert out.outcome_status == "inconclusive"
    assert out.checked_at is not None


def test_cancel(mem_session):
    make_user(mem_session)
    r = _create(mem_session)
    assert rl.cancel(mem_session, r.id).status == "cancelled"


def test_resolution_on_missing_id_returns_none(mem_session):
    make_user(mem_session)
    assert rl.mark_checked(mem_session, 9999, outcome_status="improved") is None
    assert rl.mark_inconclusive(mem_session, 9999) is None
    assert rl.cancel(mem_session, 9999) is None


# --- recent ordering --------------------------------------------------------

def test_get_recent_newest_first_and_since_filter(mem_session):
    make_user(mem_session)
    old = _create(mem_session, title="old", local_date=TODAY - timedelta(days=10))
    mid = _create(mem_session, title="mid", recommendation_type="sleep", local_date=TODAY - timedelta(days=3))
    new = _create(mem_session, title="new", recommendation_type="recovery", local_date=TODAY)
    recent = rl.get_recent(mem_session, 1)
    assert [r.id for r in recent] == [new.id, mid.id, old.id]  # newest (highest id) first
    since = rl.get_recent(mem_session, 1, since=TODAY - timedelta(days=5))
    assert {r.id for r in since} == {mid.id, new.id}


# --- dedup ------------------------------------------------------------------

def test_dedup_same_day_same_type_same_title(mem_session):
    make_user(mem_session)
    first = _create(mem_session)
    again = _create(mem_session, title="keep strain under 10 today.")  # case/punct differ
    assert again.id == first.id
    assert mem_session.query(RecommendationLedger).count() == 1


def test_dedup_allows_different_type_or_title(mem_session):
    make_user(mem_session)
    _create(mem_session)  # training
    _create(mem_session, recommendation_type="sleep", title="Get to bed by 11")  # diff type+title
    _create(mem_session, title="Add a mobility session")  # same type, diff title
    assert mem_session.query(RecommendationLedger).count() == 3


def test_dedup_can_be_disabled(mem_session):
    make_user(mem_session)
    _create(mem_session)
    _create(mem_session, dedupe=False)
    assert mem_session.query(RecommendationLedger).count() == 2


def test_dedup_ignores_resolved_recommendations(mem_session):
    make_user(mem_session)
    first = _create(mem_session)
    rl.cancel(mem_session, first.id)
    # a fresh identical one should be allowed since the old one is no longer pending
    again = _create(mem_session)
    assert again.id != first.id


# --- serialization ----------------------------------------------------------

def test_serialize_shape(mem_session):
    make_user(mem_session)
    r = _create(
        mem_session,
        reason="HRV 16% below baseline",
        expected_outcome="recovery stabilizes tomorrow",
        checkpoint_metric="recovery_score",
        checkpoint_date=TODAY + timedelta(days=1),
        tags=["hrv"],
    )
    out = rl.serialize(r)
    assert out["id"] == r.id
    assert out["type"] == "training"
    assert out["recommendation"] == r.recommendation_text
    assert out["status"] == "pending"
    assert out["reason"] == "HRV 16% below baseline"
    assert out["checkpoint_metric"] == "recovery_score"
    assert out["tags"] == ["hrv"]
    # unknown follow/outcome omitted while still pending
    assert "followed" not in out
    assert "outcome" not in out


def test_serialize_includes_resolved_fields(mem_session):
    make_user(mem_session)
    r = _create(mem_session)
    rl.mark_checked(mem_session, r.id, outcome_status="improved", followed_status="followed")
    out = rl.serialize(rl.get_recommendation(mem_session, r.id))
    assert out["status"] == "checked"
    assert out["followed"] == "followed"
    assert out["outcome"] == "improved"


# --- migration chain --------------------------------------------------------

def test_migration_0012_chains_after_0011():
    path = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0012_recommendation_ledger.py"
    spec = importlib.util.spec_from_file_location("mig_0012", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "0012"
    assert mod.down_revision == "0011"
