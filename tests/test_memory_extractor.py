"""Tests for structured memory extraction (Memory 2.0 Phase 2A).

store_candidates is deterministic (no AI) and gets the bulk of the coverage.
ai_client.extract_memories parsing is tested with a mocked OpenAI client.
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta

from app.models.user_memory import UserMemory
from app.services import ai_client, memory_extractor, memory_store
from tests.conftest import make_user

TODAY = date(2026, 6, 17)


def _candidate(**over):
    base = {
        "type": "preference",
        "content": "Prefers blunt, direct coaching",
        "confidence": "high",
        "tags": ["style"],
        "ttl_days": None,
        "should_store": True,
    }
    base.update(over)
    return base


# --- store_candidates: keepers ----------------------------------------------

def test_preference_is_stored(mem_session):
    make_user(mem_session)
    summary = memory_extractor.store_candidates(mem_session, 1, [_candidate()], today=TODAY)
    assert summary["stored"] == 1
    rows = memory_store.get_active(mem_session, 1)
    assert len(rows) == 1
    assert rows[0].memory_type == "preference"
    assert rows[0].confidence == "high"
    assert rows[0].source == "ai_extracted"


def test_goal_is_stored(mem_session):
    make_user(mem_session)
    memory_extractor.store_candidates(
        mem_session, 1, [_candidate(type="goal", content="Wants to reach 185 lbs", tags=["weight"])], today=TODAY
    )
    rows = memory_store.get_active(mem_session, 1, types=["goal"])
    assert len(rows) == 1
    assert rows[0].content == "Wants to reach 185 lbs"


def test_temporary_context_gets_expiration(mem_session):
    make_user(mem_session)
    memory_extractor.store_candidates(
        mem_session, 1,
        [_candidate(type="commitment", content="In bed by 11 this week", ttl_days=7)],
        today=TODAY,
    )
    row = memory_store.get_active(mem_session, 1)[0]
    assert row.expires_at == TODAY + timedelta(days=7)


def test_context_event_without_ttl_gets_default_expiration(mem_session):
    make_user(mem_session)
    memory_extractor.store_candidates(
        mem_session, 1,
        [_candidate(type="context_event", content="Traveling for work", ttl_days=None)],
        today=TODAY,
    )
    row = memory_store.get_active(mem_session, 1)[0]
    assert row.expires_at == TODAY + timedelta(days=memory_extractor._DEFAULT_TTL_DAYS)


def test_durable_memory_has_no_expiration(mem_session):
    make_user(mem_session)
    memory_extractor.store_candidates(
        mem_session, 1, [_candidate(type="stable_fact", content="Takes creatine daily")], today=TODAY
    )
    assert memory_store.get_active(mem_session, 1)[0].expires_at is None


def test_invalid_confidence_falls_back_to_source_default(mem_session):
    make_user(mem_session)
    memory_extractor.store_candidates(
        mem_session, 1, [_candidate(confidence="banana")], today=TODAY
    )
    # ai_extracted source default is "low"
    assert memory_store.get_active(mem_session, 1)[0].confidence == "low"


# --- store_candidates: rejects ----------------------------------------------

def test_should_store_false_is_skipped(mem_session):
    make_user(mem_session)
    summary = memory_extractor.store_candidates(
        mem_session, 1, [_candidate(should_store=False)], today=TODAY
    )
    assert summary["skipped"] == 1
    assert memory_store.get_active(mem_session, 1) == []


def test_invalid_type_is_skipped(mem_session):
    make_user(mem_session)
    summary = memory_extractor.store_candidates(
        mem_session, 1, [_candidate(type="confirmed_rule")], today=TODAY  # system-only type
    )
    assert summary["skipped"] == 1
    assert memory_store.get_active(mem_session, 1) == []


def test_empty_content_is_skipped(mem_session):
    make_user(mem_session)
    summary = memory_extractor.store_candidates(mem_session, 1, [_candidate(content="  ")], today=TODAY)
    assert summary["skipped"] == 1


def test_medical_diagnosis_is_skipped(mem_session):
    make_user(mem_session)
    summary = memory_extractor.store_candidates(
        mem_session, 1,
        [_candidate(type="stable_fact", content="Was diagnosed with depression last year")],
        today=TODAY,
    )
    assert summary["skipped"] == 1
    assert memory_store.get_active(mem_session, 1) == []


def test_noisy_batch_stores_nothing(mem_session):
    make_user(mem_session)
    noisy = [
        _candidate(should_store=False, content="thanks!"),
        _candidate(should_store=False, content="ok cool"),
    ]
    summary = memory_extractor.store_candidates(mem_session, 1, noisy, today=TODAY)
    assert summary["stored"] == 0 and summary["merged"] == 0
    assert memory_store.get_active(mem_session, 1) == []


# --- dedup ------------------------------------------------------------------

def test_duplicate_merges_instead_of_duplicating(mem_session):
    make_user(mem_session)
    memory_extractor.store_candidates(mem_session, 1, [_candidate()], today=TODAY)
    summary = memory_extractor.store_candidates(
        mem_session, 1, [_candidate(content="prefers blunt, direct coaching.")], today=TODAY
    )
    assert summary["merged"] == 1
    rows = mem_session.query(UserMemory).all()
    assert len(rows) == 1
    assert rows[0].evidence_count == 2


# --- ai_client.extract_memories parsing (mocked OpenAI) ---------------------

class _Resp:
    def __init__(self, text):
        self.output_text = text
        self.status = "completed"
        self.usage = None


class _Responses:
    def __init__(self, text):
        self._text = text

    async def create(self, **kwargs):
        return _Resp(self._text)


class _Client:
    def __init__(self, text):
        self.responses = _Responses(text)


def test_extract_memories_parses_valid_json(monkeypatch):
    payload = '{"memories": [{"type": "goal", "content": "Run a 5k", "confidence": "high", "should_store": true}]}'
    monkeypatch.setattr(ai_client, "_client", _Client(payload))
    out = asyncio.run(ai_client.extract_memories("I want to run a 5k", "nice", [], {}, user_id=1))
    assert isinstance(out, list) and len(out) == 1
    assert out[0]["type"] == "goal"


def test_extract_memories_junk_returns_empty(monkeypatch):
    monkeypatch.setattr(ai_client, "_client", _Client("not json at all"))
    out = asyncio.run(ai_client.extract_memories("hi", "hello", [], {}, user_id=1))
    assert out == []


def test_extract_memories_strips_code_fences(monkeypatch):
    payload = '```json\n{"memories": []}\n```'
    monkeypatch.setattr(ai_client, "_client", _Client(payload))
    out = asyncio.run(ai_client.extract_memories("hi", "hello", [], {}, user_id=1))
    assert out == []
