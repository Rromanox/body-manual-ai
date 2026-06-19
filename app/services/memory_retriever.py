"""MemoryRetriever: select the relevant memories for each AI context.

Deterministic SQL filters + simple keyword scoring — no embeddings, no pgvector.
Returns compact, capped, serialized dicts ready to drop into a payload.

Phase 2A wires only `for_qa` and `for_morning` into the read path. The other
contexts (weekly/manual/focus/memory_review) are implemented and tested so
Phase 2B can switch them on without changing them here.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.models.user_memory import UserMemory
from app.services import memory_store

_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}

# Durable, decision-shaping types worth surfacing in the morning message.
_MORNING_TYPES = (
    "goal",
    "preference",
    "constraint",
    "commitment",
    "context_event",
    "disliked_advice",
    "training_preference",
    "schedule_pattern",
)

# Tiny stopword set so keyword overlap keys on meaningful words.
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were", "be", "been",
    "to", "of", "in", "on", "for", "with", "my", "me", "i", "you", "your", "it",
    "this", "that", "these", "those", "do", "does", "did", "how", "what", "why",
    "when", "should", "can", "could", "would", "will", "am", "have", "has", "had",
    "about", "im", "ive", "get", "got", "any", "some", "at", "as", "if", "so",
})


def _serialize(m: UserMemory) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": m.id,
        "type": m.memory_type,
        "content": m.content,
        "confidence": m.confidence,
    }
    if m.tags:
        out["tags"] = list(m.tags)
    if m.expires_at:
        out["expires_at"] = str(m.expires_at)
    return out


def _active_unexpired(
    session: Session,
    user_id: int,
    *,
    types: tuple[str, ...] | None = None,
    today: date | None = None,
) -> list[UserMemory]:
    today = today or date.today()
    rows = memory_store.get_active(session, user_id, types=types)
    return [m for m in rows if m.expires_at is None or m.expires_at >= today]


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if len(w) >= 3 and w not in _STOPWORDS}


def for_morning(
    session: Session, user_id: int, *, limit: int = 8, today: date | None = None
) -> list[dict[str, Any]]:
    """Durable, decision-shaping memories for the morning message.

    Conservative: capped, highest-confidence and most-recent first.
    """
    rows = _active_unexpired(session, user_id, types=_MORNING_TYPES, today=today)
    rows.sort(key=lambda m: (_CONFIDENCE_RANK.get(m.confidence, 0), m.id), reverse=True)
    return [_serialize(m) for m in rows[:limit]]


def for_qa(
    session: Session,
    user_id: int,
    question: str,
    *,
    limit: int = 8,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Memories relevant to a question, ranked by keyword overlap then confidence.

    Memories with no keyword overlap can still appear (durable context like goals)
    but rank below anything the question actually mentions.
    """
    rows = _active_unexpired(session, user_id, today=today)
    if not rows:
        return []
    q_tokens = _tokens(question)

    def score(m: UserMemory) -> tuple[int, int, int]:
        mem_tokens = _tokens(m.content) | {str(t).lower() for t in (m.tags or [])}
        overlap = len(q_tokens & mem_tokens)
        return (overlap, _CONFIDENCE_RANK.get(m.confidence, 0), m.id)

    rows.sort(key=score, reverse=True)
    return [_serialize(m) for m in rows[:limit]]


def for_weekly(
    session: Session, user_id: int, *, limit: int = 10, today: date | None = None
) -> list[dict[str, Any]]:
    """High-confidence durable memories only — for weekly synthesis (Phase 2B wiring)."""
    rows = _active_unexpired(session, user_id, today=today)
    rows = [m for m in rows if m.confidence == "high"]
    rows.sort(key=lambda m: m.id, reverse=True)
    return [_serialize(m) for m in rows[:limit]]


def for_manual(
    session: Session, user_id: int, *, limit: int = 20, today: date | None = None
) -> list[dict[str, Any]]:
    """Medium+high confidence memories — for the Body Manual (Phase 2B wiring)."""
    rows = _active_unexpired(session, user_id, today=today)
    rows = [m for m in rows if _CONFIDENCE_RANK.get(m.confidence, 0) >= 1]
    rows.sort(key=lambda m: (_CONFIDENCE_RANK.get(m.confidence, 0), m.id), reverse=True)
    return [_serialize(m) for m in rows[:limit]]


def for_focus(
    session: Session, user_id: int, *, limit: int = 5, today: date | None = None
) -> list[dict[str, Any]]:
    """Top constraints/goals/preferences for /focus (Phase 2B wiring)."""
    rows = _active_unexpired(
        session, user_id, types=("constraint", "goal", "preference"), today=today
    )
    rows.sort(key=lambda m: (_CONFIDENCE_RANK.get(m.confidence, 0), m.id), reverse=True)
    return [_serialize(m) for m in rows[:limit]]


def for_memory_review(
    session: Session, user_id: int, *, limit: int = 30, today: date | None = None
) -> list[dict[str, Any]]:
    """Everything active and unexpired — for the future weekly memory review."""
    rows = _active_unexpired(session, user_id, today=today)
    rows.sort(key=lambda m: m.id, reverse=True)
    return [_serialize(m) for m in rows[:limit]]


# --- /memory command rendering (pure, testable) -----------------------------

_TYPE_LABELS = {
    "stable_fact": "Facts",
    "preference": "Preferences",
    "constraint": "Constraints",
    "goal": "Goals",
    "commitment": "Commitments",
    "context_event": "Current context",
    "disliked_advice": "Disliked advice",
    "hypothesis": "Hypotheses",
    "training_preference": "Training",
    "schedule_pattern": "Schedule",
    "recovery_trigger": "Recovery triggers",
    "weight_context": "Weight context",
    "food_pattern": "Food patterns",
    "sleep_pattern": "Sleep patterns",
    "confirmed_rule": "Rules",
}


def render_memory_list(memories: list[UserMemory], header: str) -> str:
    """Group memories by type into a plain-text Telegram string. Pure function.

    Plain text (no Markdown) on purpose: memory content is AI-extracted free text
    and would routinely break Telegram's Markdown parser.
    """
    if not memories:
        return f"{header}\n\nNothing yet — I'll learn as we talk."
    by_type: dict[str, list[UserMemory]] = {}
    for m in memories:
        by_type.setdefault(m.memory_type, []).append(m)

    lines = [header, ""]
    for mem_type, label in _TYPE_LABELS.items():
        group = by_type.get(mem_type)
        if not group:
            continue
        lines.append(f"{label}:")
        for m in group:
            conf = "" if m.confidence == "medium" else f" ({m.confidence})"
            lines.append(f"  [{m.id}] {m.content}{conf}")
        lines.append("")
    return "\n".join(lines).rstrip()
