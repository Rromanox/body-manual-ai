"""MemoryStore: the only writer/reader of the user_memories table.

Deterministic CRUD + dedup/merge for structured memory (Memory 2.0 Phase 1).
No AI here — extraction and relevance ranking arrive in later phases. This layer
just stores typed facts, deduplicates them, and supports correction
(archive / confirm / supersede / merge).

Phase 1 scope: nothing user-facing reads this yet. about_you / coach_notes stay
the live memory until a later phase wires retrieval in.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.user_memory import UserMemory

# Allowed memory types (validated in code, not a DB enum, so adding one needs no
# migration). Mirrors Memory 2.0 plan §4.1.
MEMORY_TYPES: frozenset[str] = frozenset({
    "stable_fact",
    "preference",
    "constraint",
    "goal",
    "commitment",
    "context_event",
    "disliked_advice",
    "hypothesis",
    "confirmed_rule",
    "training_preference",
    "schedule_pattern",
    "recovery_trigger",
    "weight_context",
    "food_pattern",
    "sleep_pattern",
})

VALID_SOURCES: frozenset[str] = frozenset({"user_stated", "ai_extracted", "derived"})
VALID_CONFIDENCE: frozenset[str] = frozenset({"low", "medium", "high"})

# Default confidence when the caller doesn't specify one, keyed by source (§13).
_CONFIDENCE_BY_SOURCE: dict[str, str] = {
    "user_stated": "medium",
    "ai_extracted": "low",
    "derived": "medium",
}

# Repeated reinforcement raises a low-confidence memory to medium once it has been
# seen this many times.
_EVIDENCE_CONFIDENCE_FLOOR = 3

_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def _normalize(text: str | None) -> str:
    """Loose normalization for dedup: case/whitespace/trailing-punctuation-insensitive."""
    collapsed = re.sub(r"\s+", " ", (text or "").strip().lower())
    return collapsed.rstrip(".!?,;: ")


def add_memory(
    session: Session,
    user_id: int,
    memory_type: str,
    content: str,
    *,
    structured: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    source: str = "ai_extracted",
    confidence: str | None = None,
    expires_at: date | None = None,
    last_seen_at: date | None = None,
    evidence_count: int = 1,
    dedupe: bool = True,
    commit: bool = True,
) -> UserMemory:
    """Insert a memory, or reinforce an existing duplicate.

    When ``dedupe`` is True and an active memory of the same type with
    normalized-equal content already exists, that row is reinforced
    (evidence_count bumped, last_seen refreshed, confidence possibly raised) and
    returned instead of inserting a second copy — this is what makes the
    coach_notes migration and repeated extraction idempotent.
    """
    if memory_type not in MEMORY_TYPES:
        raise ValueError(f"Unknown memory_type: {memory_type!r}")
    if source not in VALID_SOURCES:
        raise ValueError(f"Unknown source: {source!r}")
    if confidence is not None and confidence not in VALID_CONFIDENCE:
        raise ValueError(f"Unknown confidence: {confidence!r}")

    content = (content or "").strip()
    if not content:
        raise ValueError("Memory content cannot be empty")

    resolved_confidence = confidence or _CONFIDENCE_BY_SOURCE.get(source, "low")
    today = last_seen_at or date.today()

    if dedupe:
        existing = _find_duplicate(session, user_id, memory_type, content)
        if existing is not None:
            existing.evidence_count += evidence_count
            existing.last_seen_at = today
            # Reinforcement can raise confidence, never lower it.
            if (
                existing.confidence == "low"
                and existing.evidence_count >= _EVIDENCE_CONFIDENCE_FLOOR
            ):
                existing.confidence = "medium"
            if _CONFIDENCE_RANK[resolved_confidence] > _CONFIDENCE_RANK[existing.confidence]:
                existing.confidence = resolved_confidence
            if tags:
                merged = list(existing.tags or [])
                for t in tags:
                    if t not in merged:
                        merged.append(t)
                existing.tags = merged
            if commit:
                session.commit()
            return existing

    memory = UserMemory(
        user_id=user_id,
        memory_type=memory_type,
        content=content,
        structured=structured or {},
        tags=tags or [],
        source=source,
        confidence=resolved_confidence,
        evidence_count=evidence_count,
        expires_at=expires_at,
        last_seen_at=today,
        status="active",
    )
    session.add(memory)
    if commit:
        session.commit()
    else:
        session.flush()
    return memory


def _find_duplicate(
    session: Session, user_id: int, memory_type: str, content: str
) -> UserMemory | None:
    norm = _normalize(content)
    candidates = session.scalars(
        select(UserMemory).where(
            UserMemory.user_id == user_id,
            UserMemory.memory_type == memory_type,
            UserMemory.status == "active",
        )
    ).all()
    for c in candidates:
        if _normalize(c.content) == norm:
            return c
    return None


def get_memory(session: Session, memory_id: int) -> UserMemory | None:
    return session.get(UserMemory, memory_id)


def get_active(
    session: Session,
    user_id: int,
    *,
    types: Iterable[str] | None = None,
    tags: Iterable[str] | None = None,
    limit: int | None = None,
) -> list[UserMemory]:
    """Active memories for a user, newest first. Optional type/tag filtering.

    Tag filtering is an OR/overlap match (any shared tag), applied in Python so
    it behaves identically on Postgres and SQLite.
    """
    stmt = select(UserMemory).where(
        UserMemory.user_id == user_id,
        UserMemory.status == "active",
    )
    if types is not None:
        type_list = list(types)
        if type_list:
            stmt = stmt.where(UserMemory.memory_type.in_(type_list))
    stmt = stmt.order_by(UserMemory.id.desc())

    rows = list(session.scalars(stmt).all())

    if tags is not None:
        wanted = set(tags)
        rows = [r for r in rows if wanted & set(r.tags or [])]

    if limit is not None:
        rows = rows[:limit]
    return rows


def archive(session: Session, memory_id: int, *, commit: bool = True) -> bool:
    """Soft-delete a memory so it stops surfacing. Returns False if not found."""
    memory = session.get(UserMemory, memory_id)
    if memory is None:
        return False
    memory.status = "archived"
    if commit:
        session.commit()
    return True


def confirm(session: Session, memory_id: int, *, commit: bool = True) -> UserMemory | None:
    """User-confirmed a memory: promote to high confidence + user_stated source."""
    memory = session.get(UserMemory, memory_id)
    if memory is None:
        return None
    memory.confidence = "high"
    memory.source = "user_stated"
    memory.last_seen_at = date.today()
    if commit:
        session.commit()
    return memory


def supersede(session: Session, old_id: int, new_id: int, *, commit: bool = True) -> bool:
    """Mark old_id as replaced by new_id (correction/merge chain).

    Returns False if either id is missing. Does not touch new_id's status.
    """
    old = session.get(UserMemory, old_id)
    new = session.get(UserMemory, new_id)
    if old is None or new is None:
        return False
    old.status = "superseded"
    old.superseded_by = new_id
    if commit:
        session.commit()
    return True


def merge_duplicates(session: Session, user_id: int, *, commit: bool = True) -> int:
    """Collapse active same-type, normalized-equal memories into one.

    Keeps the earliest row (lowest id), sums evidence into it, and supersedes the
    rest. Deterministic — no AI. Returns the number of rows superseded.
    """
    rows = session.scalars(
        select(UserMemory)
        .where(UserMemory.user_id == user_id, UserMemory.status == "active")
        .order_by(UserMemory.id.asc())
    ).all()

    groups: dict[tuple[str, str], list[UserMemory]] = {}
    for r in rows:
        key = (r.memory_type, _normalize(r.content))
        groups.setdefault(key, []).append(r)

    merged = 0
    for group in groups.values():
        if len(group) < 2:
            continue
        keeper = group[0]
        for dup in group[1:]:
            keeper.evidence_count += dup.evidence_count
            if _CONFIDENCE_RANK[dup.confidence] > _CONFIDENCE_RANK[keeper.confidence]:
                keeper.confidence = dup.confidence
            for t in (dup.tags or []):
                existing_tags = list(keeper.tags or [])
                if t not in existing_tags:
                    keeper.tags = existing_tags + [t]
            dup.status = "superseded"
            dup.superseded_by = keeper.id
            merged += 1

    if commit and merged:
        session.commit()
    return merged
