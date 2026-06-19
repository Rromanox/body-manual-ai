"""Unit tests for memory_store — structured memory CRUD + dedup/merge (Phase 1)."""
from __future__ import annotations

from datetime import date

import pytest

from app.models.user_memory import UserMemory
from app.services import memory_store
from tests.conftest import make_user


# --- add_memory -------------------------------------------------------------

def test_add_memory_creates_row(mem_session):
    make_user(mem_session)
    mem = memory_store.add_memory(
        mem_session, 1, "preference", "Prefers blunt, direct coaching", tags=["style"]
    )
    assert mem.id is not None
    assert mem.memory_type == "preference"
    assert mem.status == "active"
    assert mem.tags == ["style"]
    assert mem.evidence_count == 1


def test_add_memory_rejects_unknown_type(mem_session):
    make_user(mem_session)
    with pytest.raises(ValueError):
        memory_store.add_memory(mem_session, 1, "not_a_type", "x")


def test_add_memory_rejects_empty_content(mem_session):
    make_user(mem_session)
    with pytest.raises(ValueError):
        memory_store.add_memory(mem_session, 1, "stable_fact", "   ")


def test_confidence_defaults_from_source(mem_session):
    make_user(mem_session)
    ai = memory_store.add_memory(mem_session, 1, "stable_fact", "takes creatine", source="ai_extracted")
    stated = memory_store.add_memory(mem_session, 1, "goal", "lose weight", source="user_stated")
    assert ai.confidence == "low"
    assert stated.confidence == "medium"


# --- dedup ------------------------------------------------------------------

def test_dedup_reinforces_instead_of_inserting(mem_session):
    make_user(mem_session)
    first = memory_store.add_memory(mem_session, 1, "stable_fact", "Takes creatine daily")
    second = memory_store.add_memory(mem_session, 1, "stable_fact", "takes creatine daily.")  # case/punct differ
    assert first.id == second.id
    assert second.evidence_count == 2
    assert mem_session.query(UserMemory).count() == 1


def test_dedup_bumps_low_confidence_to_medium_at_threshold(mem_session):
    make_user(mem_session)
    mem = memory_store.add_memory(mem_session, 1, "stable_fact", "asthmatic", source="ai_extracted")
    assert mem.confidence == "low"
    memory_store.add_memory(mem_session, 1, "stable_fact", "asthmatic")
    memory_store.add_memory(mem_session, 1, "stable_fact", "asthmatic")  # 3rd sighting
    assert mem.evidence_count == 3
    assert mem.confidence == "medium"


def test_dedup_can_be_disabled(mem_session):
    make_user(mem_session)
    memory_store.add_memory(mem_session, 1, "stable_fact", "same", dedupe=False)
    memory_store.add_memory(mem_session, 1, "stable_fact", "same", dedupe=False)
    assert mem_session.query(UserMemory).count() == 2


def test_dedup_is_scoped_per_type_and_user(mem_session):
    make_user(mem_session, user_id=1)
    make_user(mem_session, user_id=2)
    memory_store.add_memory(mem_session, 1, "stable_fact", "runs daily")
    # different type -> not a duplicate
    memory_store.add_memory(mem_session, 1, "preference", "runs daily")
    # different user -> not a duplicate
    memory_store.add_memory(mem_session, 2, "stable_fact", "runs daily")
    assert mem_session.query(UserMemory).count() == 3


# --- get_active -------------------------------------------------------------

def test_get_active_excludes_non_active(mem_session):
    make_user(mem_session)
    keep = memory_store.add_memory(mem_session, 1, "stable_fact", "keep")
    drop = memory_store.add_memory(mem_session, 1, "stable_fact", "drop")
    memory_store.archive(mem_session, drop.id)
    active = memory_store.get_active(mem_session, 1)
    assert [m.id for m in active] == [keep.id]


def test_get_active_filters_by_type(mem_session):
    make_user(mem_session)
    memory_store.add_memory(mem_session, 1, "goal", "lose weight")
    memory_store.add_memory(mem_session, 1, "preference", "blunt coaching")
    goals = memory_store.get_active(mem_session, 1, types=["goal"])
    assert len(goals) == 1
    assert goals[0].memory_type == "goal"


def test_get_active_filters_by_tag_overlap(mem_session):
    make_user(mem_session)
    memory_store.add_memory(mem_session, 1, "stable_fact", "creatine", tags=["supplement"])
    memory_store.add_memory(mem_session, 1, "stable_fact", "trains at night", tags=["lifestyle"])
    supps = memory_store.get_active(mem_session, 1, tags=["supplement"])
    assert len(supps) == 1
    assert supps[0].content == "creatine"


def test_get_active_respects_limit(mem_session):
    make_user(mem_session)
    for i in range(5):
        memory_store.add_memory(mem_session, 1, "stable_fact", f"fact {i}")
    assert len(memory_store.get_active(mem_session, 1, limit=3)) == 3


# --- archive / confirm / supersede ------------------------------------------

def test_archive(mem_session):
    make_user(mem_session)
    mem = memory_store.add_memory(mem_session, 1, "stable_fact", "x")
    assert memory_store.archive(mem_session, mem.id) is True
    assert mem.status == "archived"
    assert memory_store.archive(mem_session, 9999) is False


def test_confirm_raises_confidence_and_source(mem_session):
    make_user(mem_session)
    mem = memory_store.add_memory(mem_session, 1, "stable_fact", "x", source="ai_extracted")
    confirmed = memory_store.confirm(mem_session, mem.id)
    assert confirmed.confidence == "high"
    assert confirmed.source == "user_stated"
    assert memory_store.confirm(mem_session, 9999) is None


def test_supersede_marks_old(mem_session):
    make_user(mem_session)
    old = memory_store.add_memory(mem_session, 1, "goal", "lose 10 lbs")
    new = memory_store.add_memory(mem_session, 1, "goal", "lose 20 lbs")
    assert memory_store.supersede(mem_session, old.id, new.id) is True
    assert old.status == "superseded"
    assert old.superseded_by == new.id
    assert new.status == "active"
    # superseded rows drop out of get_active
    assert {m.id for m in memory_store.get_active(mem_session, 1)} == {new.id}


def test_supersede_missing_returns_false(mem_session):
    make_user(mem_session)
    mem = memory_store.add_memory(mem_session, 1, "goal", "x")
    assert memory_store.supersede(mem_session, mem.id, 9999) is False


# --- merge_duplicates -------------------------------------------------------

def test_merge_duplicates_collapses_and_sums_evidence(mem_session):
    make_user(mem_session)
    a = memory_store.add_memory(mem_session, 1, "stable_fact", "drinks coffee", dedupe=False)
    b = memory_store.add_memory(mem_session, 1, "stable_fact", "Drinks coffee.", dedupe=False)
    c = memory_store.add_memory(mem_session, 1, "stable_fact", "drinks coffee", dedupe=False)
    merged = memory_store.merge_duplicates(mem_session, 1)
    assert merged == 2
    assert a.status == "active"
    assert b.status == "superseded"
    assert c.status == "superseded"
    assert a.evidence_count == 3  # 1 + 1 + 1


def test_merge_duplicates_noop_when_unique(mem_session):
    make_user(mem_session)
    memory_store.add_memory(mem_session, 1, "stable_fact", "alpha")
    memory_store.add_memory(mem_session, 1, "stable_fact", "beta")
    assert memory_store.merge_duplicates(mem_session, 1) == 0
