"""Manual, one-shot migration: users.coach_notes blob -> typed user_memories.

NON-DESTRUCTIVE and IDEMPOTENT by design:
- It NEVER modifies or clears users.coach_notes (about_you keeps working).
- Re-running is safe: writes go through memory_store.add_memory(dedupe=True), so
  an already-migrated fact is reinforced, not duplicated.
- Dry-run is the default. You must pass --apply to write anything.

This is deliberately NOT wired into app startup or Alembic (Memory 2.0 plan:
"manual one-shot you trigger after deploy"). Run it yourself and verify.

Usage (from the repo root, with the venv active):
    python -m app.services.migrate_coach_notes              # dry run, all users
    python -m app.services.migrate_coach_notes --apply      # write, all users
    python -m app.services.migrate_coach_notes --user 1     # limit to one user id
"""
from __future__ import annotations

import argparse
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.user import User
from app.services import memory_store

logger = logging.getLogger(__name__)

# coach_notes keys (set by ai_client.FACT_EXTRACTOR_SYSTEM_PROMPT) -> memory type + tag.
_KEY_MAP: dict[str, tuple[str, str]] = {
    "supplements": ("stable_fact", "supplement"),
    "medications": ("stable_fact", "medication"),
    "health_context": ("stable_fact", "health"),
    "goals": ("goal", "goal"),
    # "lifestyle" and "other" are routed by content keywords below.
}

# lifestyle content routing.
_TRAINING_KEYWORDS = ("gym", "train", "workout", "work out", "lift", "run", "cardio", "exercise")
_SCHEDULE_KEYWORDS = ("shift", "night", "morning", "schedule", "9-5", "work day", "commute")

# "other" content that reads as temporary -> context_event (gets no expiry here;
# expiry handling is a later phase, but the type is correct).
_TEMPORARY_KEYWORDS = (
    "until", "this week", "this month", "this season", "temporar",
    "currently", "for now", "right now", "these days",
)

# coach_notes facts originated from the user but were AI-extracted into the blob,
# so they carry medium confidence on import (more than fresh extraction, less than
# explicitly confirmed). Source reflects how they got here.
_IMPORT_SOURCE = "ai_extracted"
_IMPORT_CONFIDENCE = "medium"


def _classify(key: str, content: str) -> dict[str, Any]:
    """Map one coach_notes (key, value) into a memory candidate dict."""
    lower = content.lower()

    if key in _KEY_MAP:
        memory_type, tag = _KEY_MAP[key]
    elif key == "lifestyle":
        if any(k in lower for k in _TRAINING_KEYWORDS):
            memory_type = "training_preference"
        elif any(k in lower for k in _SCHEDULE_KEYWORDS):
            memory_type = "schedule_pattern"
        else:
            memory_type = "stable_fact"
        tag = "lifestyle"
    else:  # "other" or any unexpected key
        if any(k in lower for k in _TEMPORARY_KEYWORDS):
            memory_type = "context_event"
        else:
            memory_type = "stable_fact"
        tag = "misc"

    return {
        "memory_type": memory_type,
        "content": content,
        "tags": [tag],
        "source": _IMPORT_SOURCE,
        "confidence": _IMPORT_CONFIDENCE,
    }


def convert_coach_notes(notes: Any) -> list[dict[str, Any]]:
    """Pure function: a coach_notes blob -> list of memory candidate dicts.

    Handles both list-valued keys (the normal shape) and scalar string values.
    Returns [] for anything empty or malformed. No DB access — fully testable.
    """
    out: list[dict[str, Any]] = []
    if not isinstance(notes, dict):
        return out
    for key, raw in notes.items():
        items = raw if isinstance(raw, list) else [raw]
        for item in items:
            content = str(item).strip() if item is not None else ""
            if not content:
                continue
            out.append(_classify(str(key), content))
    return out


def migrate_user(
    session: Session,
    user: User,
    *,
    dry_run: bool = True,
) -> list[dict[str, Any]]:
    """Convert one user's coach_notes into user_memories.

    Returns the list of candidate dicts that were (or, in dry-run, would be)
    written. Never modifies user.coach_notes. Idempotent via add_memory dedup.
    """
    candidates = convert_coach_notes(user.coach_notes)
    if dry_run:
        return candidates

    for c in candidates:
        memory_store.add_memory(
            session,
            user.id,
            c["memory_type"],
            c["content"],
            tags=c["tags"],
            source=c["source"],
            confidence=c["confidence"],
            dedupe=True,
            commit=False,
        )
    session.commit()
    return candidates


def migrate_all(
    session: Session,
    *,
    dry_run: bool = True,
    user_id: int | None = None,
) -> dict[int, list[dict[str, Any]]]:
    """Migrate every user with a non-empty coach_notes blob (or one user)."""
    stmt = select(User)
    if user_id is not None:
        stmt = stmt.where(User.id == user_id)
    users = session.scalars(stmt).all()

    summary: dict[int, list[dict[str, Any]]] = {}
    for user in users:
        if not user.coach_notes:
            continue
        summary[user.id] = migrate_user(session, user, dry_run=dry_run)
    return summary


def _main() -> None:
    parser = argparse.ArgumentParser(description="Migrate coach_notes -> user_memories (manual, idempotent).")
    parser.add_argument("--apply", action="store_true", help="Actually write rows. Without this it's a dry run.")
    parser.add_argument("--user", type=int, default=None, help="Limit to a single user id.")
    args = parser.parse_args()

    dry_run = not args.apply
    mode = "DRY RUN (no writes)" if dry_run else "APPLY (writing rows)"
    print(f"coach_notes -> user_memories — {mode}")

    with SessionLocal() as session:
        summary = migrate_all(session, dry_run=dry_run, user_id=args.user)

    if not summary:
        print("No users with coach_notes to migrate.")
        return

    total = 0
    for uid, candidates in summary.items():
        print(f"\nUser {uid}: {len(candidates)} memor{'y' if len(candidates) == 1 else 'ies'}")
        for c in candidates:
            print(f"  [{c['memory_type']}] ({', '.join(c['tags'])}) {c['content']}")
        total += len(candidates)

    verb = "would be written" if dry_run else "written"
    print(f"\nTotal: {total} memories {verb} across {len(summary)} user(s).")
    if dry_run:
        print("Re-run with --apply to write them. (Safe to run repeatedly; dedup prevents duplicates.)")


if __name__ == "__main__":
    _main()
