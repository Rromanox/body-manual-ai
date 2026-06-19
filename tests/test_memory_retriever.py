"""Tests for MemoryRetriever (Memory 2.0 Phase 2A)."""
from __future__ import annotations

from datetime import date, timedelta

from app.services import memory_retriever, memory_store
from tests.conftest import make_user

TODAY = date(2026, 6, 17)


def _add(session, user_id, mtype, content, *, confidence="medium", tags=None, expires_at=None):
    return memory_store.add_memory(
        session, user_id, mtype, content,
        tags=tags, confidence=confidence, expires_at=expires_at, dedupe=False,
    )


# --- for_morning ------------------------------------------------------------

def test_for_morning_includes_only_morning_types(mem_session):
    make_user(mem_session)
    _add(mem_session, 1, "goal", "Reach 185 lbs")
    _add(mem_session, 1, "preference", "Blunt coaching")
    _add(mem_session, 1, "hypothesis", "Maybe coffee hurts sleep")  # not a morning type
    out = memory_retriever.for_morning(mem_session, 1, today=TODAY)
    types = {m["type"] for m in out}
    assert types == {"goal", "preference"}


def test_for_morning_excludes_expired(mem_session):
    make_user(mem_session)
    _add(mem_session, 1, "commitment", "Bed by 11", expires_at=TODAY - timedelta(days=1))
    _add(mem_session, 1, "goal", "Reach 185 lbs")
    out = memory_retriever.for_morning(mem_session, 1, today=TODAY)
    assert [m["type"] for m in out] == ["goal"]


def test_for_morning_respects_limit(mem_session):
    make_user(mem_session)
    for i in range(10):
        _add(mem_session, 1, "preference", f"pref {i}")
    out = memory_retriever.for_morning(mem_session, 1, limit=8, today=TODAY)
    assert len(out) == 8


def test_for_morning_orders_high_confidence_first(mem_session):
    make_user(mem_session)
    _add(mem_session, 1, "goal", "low one", confidence="low")
    _add(mem_session, 1, "goal", "high one", confidence="high")
    out = memory_retriever.for_morning(mem_session, 1, today=TODAY)
    assert out[0]["content"] == "high one"


# --- for_qa -----------------------------------------------------------------

def test_for_qa_ranks_keyword_matches_first(mem_session):
    make_user(mem_session)
    _add(mem_session, 1, "stable_fact", "Avoids caffeine after noon", tags=["caffeine"])
    _add(mem_session, 1, "preference", "Prefers training at night", tags=["training"])
    out = memory_retriever.for_qa(mem_session, 1, "is training at night bad for recovery?", today=TODAY)
    assert out[0]["content"] == "Prefers training at night"


def test_for_qa_excludes_expired(mem_session):
    make_user(mem_session)
    _add(mem_session, 1, "context_event", "Traveling this week", expires_at=TODAY - timedelta(days=2))
    _add(mem_session, 1, "goal", "Reach 185 lbs")
    out = memory_retriever.for_qa(mem_session, 1, "anything about travel?", today=TODAY)
    assert all("Traveling" not in m["content"] for m in out)


def test_for_qa_respects_limit(mem_session):
    make_user(mem_session)
    for i in range(12):
        _add(mem_session, 1, "stable_fact", f"fact number {i}")
    out = memory_retriever.for_qa(mem_session, 1, "fact", limit=8, today=TODAY)
    assert len(out) == 8


def test_serialize_shape(mem_session):
    make_user(mem_session)
    _add(mem_session, 1, "goal", "Reach 185 lbs", tags=["weight"], confidence="high")
    out = memory_retriever.for_qa(mem_session, 1, "weight goal?", today=TODAY)[0]
    assert set(out.keys()) >= {"id", "type", "content", "confidence"}
    assert out["tags"] == ["weight"]


# --- for_weekly (high confidence only) --------------------------------------

def test_for_weekly_high_confidence_only(mem_session):
    make_user(mem_session)
    _add(mem_session, 1, "goal", "high", confidence="high")
    _add(mem_session, 1, "goal", "medium", confidence="medium")
    out = memory_retriever.for_weekly(mem_session, 1, today=TODAY)
    assert [m["content"] for m in out] == ["high"]


# --- render_memory_list (pure) ----------------------------------------------

def test_render_groups_and_shows_ids(mem_session):
    make_user(mem_session)
    g = _add(mem_session, 1, "goal", "Reach 185 lbs")
    p = _add(mem_session, 1, "preference", "Blunt coaching")
    rows = memory_store.get_active(mem_session, 1)
    text = memory_retriever.render_memory_list(rows, "What I remember")
    assert "What I remember" in text
    assert "Goals:" in text and "Preferences:" in text
    assert f"[{g.id}]" in text and f"[{p.id}]" in text


def test_render_empty():
    text = memory_retriever.render_memory_list([], "What I remember")
    assert "Nothing yet" in text
