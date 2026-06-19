"""Structured memory extraction (Memory 2.0 Phase 2A).

The AI (ai_client.extract_memories, ModelRoute.EXTRACT) turns a chat exchange
into typed candidate dicts; this module validates them deterministically and
persists the keepers via MemoryStore. Splitting it this way keeps the rules
(what's worth storing) in testable backend code, not in the model.

This runs as a background task ALONGSIDE the legacy coach_notes extractor
(_update_coach_notes). It never modifies coach_notes and never raises into the
caller — failures are logged and swallowed.
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.user import User
from app.services import ai_client, memory_store
from app.services.timekit import get_user_now, now_block

logger = logging.getLogger(__name__)

# Types the extractor may produce — the full vocabulary MINUS confirmed_rule,
# which is system-derived (Phase 5), never AI-extracted from chat.
EXTRACTOR_TYPES: frozenset[str] = memory_store.MEMORY_TYPES - {"confirmed_rule"}

VALID_CONFIDENCE = {"low", "medium", "high"}

# Types that describe a temporary situation — they must expire. If the model
# didn't give a ttl, fall back to this so temporary context never lingers.
_TEMPORARY_TYPES = {"context_event", "commitment"}
_DEFAULT_TTL_DAYS = 30

# Backend backstop for the "never store a medical diagnosis as a fact" rule
# (the prompt also forbids it). Conservative — only clear diagnosis phrasing.
# Matches "diagnos*" (diagnose/diagnosed/diagnosis) as a prefix, or "i have <condition>".
# No trailing \b on the prefix — otherwise "diagnosed" wouldn't match mid-word.
_MEDICAL_DIAGNOSIS_RE = re.compile(
    r"\bdiagnos|\bi have (depression|anxiety|adhd|diabetes|cancer|hypertension|"
    r"an?\s+(illness|disorder|condition|disease))\b",
    re.IGNORECASE,
)

# How many existing memories to show the model as "don't repeat these".
_EXISTING_CONTEXT_LIMIT = 50


def _evaluate(candidate: dict[str, Any]) -> tuple[bool, str]:
    """Deterministic gate: should this candidate be stored? Returns (store, reason)."""
    if not isinstance(candidate, dict):
        return False, "not_a_dict"
    if candidate.get("should_store") is False:
        return False, "should_store_false"
    content = str(candidate.get("content") or "").strip()
    if not content:
        return False, "empty_content"
    mem_type = candidate.get("type")
    if mem_type not in EXTRACTOR_TYPES:
        return False, f"invalid_type:{mem_type}"
    if _MEDICAL_DIAGNOSIS_RE.search(content):
        return False, "medical_diagnosis"
    return True, "ok"


def _expires_for(candidate: dict[str, Any], mem_type: str, today: date) -> date | None:
    ttl = candidate.get("ttl_days")
    try:
        ttl_int = int(ttl) if ttl is not None else None
    except (TypeError, ValueError):
        ttl_int = None
    if ttl_int and ttl_int > 0:
        return today + timedelta(days=ttl_int)
    if mem_type in _TEMPORARY_TYPES:
        return today + timedelta(days=_DEFAULT_TTL_DAYS)
    return None


def store_candidates(
    session: Session,
    user_id: int,
    candidates: list[dict[str, Any]],
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Validate + persist candidate memories. Deterministic; no AI, no network.

    Returns a summary dict (counts + per-candidate decisions) for debugging.
    Uses MemoryStore dedup so re-extracting a known fact reinforces it.
    """
    today = today or date.today()
    stored = 0
    merged = 0
    skipped = 0
    decisions: list[dict[str, Any]] = []

    for c in candidates or []:
        keep, reason = _evaluate(c)
        snippet = str(c.get("content") or "")[:80] if isinstance(c, dict) else ""
        if not keep:
            skipped += 1
            decisions.append({"action": "skipped", "reason": reason, "content": snippet})
            logger.info("memory_extract user=%s action=skipped reason=%s content=%r", user_id, reason, snippet)
            continue

        mem_type = c["type"]
        content = str(c["content"]).strip()
        confidence = c.get("confidence")
        if confidence not in VALID_CONFIDENCE:
            confidence = None  # let MemoryStore pick the source default (ai_extracted -> low)
        tags = [str(t) for t in c.get("tags") or [] if str(t).strip()]
        expires_at = _expires_for(c, mem_type, today)

        was_dup = memory_store.find_active_duplicate(session, user_id, mem_type, content) is not None
        memory_store.add_memory(
            session,
            user_id,
            mem_type,
            content,
            tags=tags or None,
            source="ai_extracted",
            confidence=confidence,
            expires_at=expires_at,
            last_seen_at=today,
            dedupe=True,
            commit=False,
        )
        if was_dup:
            merged += 1
            action = "merged"
        else:
            stored += 1
            action = "stored"
        decisions.append({
            "action": action, "type": mem_type, "confidence": confidence or "default",
            "expires_at": str(expires_at) if expires_at else None, "content": snippet,
        })
        logger.info(
            "memory_extract user=%s action=%s type=%s confidence=%s expires=%s content=%r",
            user_id, action, mem_type, confidence or "default", expires_at, snippet,
        )

    session.commit()
    summary = {"stored": stored, "merged": merged, "skipped": skipped, "decisions": decisions}
    logger.info("memory_extract user=%s summary stored=%d merged=%d skipped=%d", user_id, stored, merged, skipped)
    return summary


async def run_for_exchange(
    user_id: int, user_message: str, ai_response: str
) -> dict[str, Any]:
    """Full background path: load context, call the EXTRACT route, store keepers.

    Crash-proof — returns {} on any failure (mirrors extract_user_facts). Safe to
    fire-and-forget from a handler.
    """
    try:
        with SessionLocal() as session:
            user = session.get(User, user_id)
            if user is None:
                return {}
            existing = memory_store.get_active(session, user_id, limit=_EXISTING_CONTEXT_LIMIT)
            existing_contents = [m.content for m in existing]
            now = get_user_now(user)
            now_b = now_block(user, now)
            today = now.date()

        candidates = await ai_client.extract_memories(
            user_message, ai_response, existing_contents, now_b, user_id=user_id
        )
        if not candidates:
            logger.info("memory_extract user=%s no candidates", user_id)
            return {"stored": 0, "merged": 0, "skipped": 0, "decisions": []}

        with SessionLocal() as session:
            return store_candidates(session, user_id, candidates, today=today)
    except Exception:
        logger.exception("memory_extract failed for user %s", user_id)
        return {}
