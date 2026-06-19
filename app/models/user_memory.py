from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, JSONVariant


class UserMemory(Base):
    """One discrete remembered fact/preference/constraint/goal/context.

    Structured replacement-superset of the loose users.coach_notes blob. Each row
    carries a type, lifecycle status, provenance, and confidence so memory can be
    queried, corrected, and surfaced selectively (Memory 2.0 plan §4.1).

    Phase 1 is the foundation only: this table is written/read solely by
    memory_store and the manual coach_notes migration. Nothing user-facing reads
    it yet — about_you / coach_notes stay the live memory until a later phase.
    """

    __tablename__ = "user_memories"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )

    # See memory_store.MEMORY_TYPES for the allowed vocabulary (validated in code,
    # not a DB enum, so new types don't need a migration).
    memory_type: Mapped[str] = mapped_column(String(32), index=True)
    content: Mapped[str] = mapped_column(Text)
    structured: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict)

    # active | watching | archived | superseded
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active")
    # user_stated | ai_extracted | derived
    source: Mapped[str] = mapped_column(String(16), default="ai_extracted", server_default="ai_extracted")
    # low | medium | high
    confidence: Mapped[str] = mapped_column(String(12), default="low", server_default="low")

    tags: Mapped[list[Any]] = mapped_column(JSONVariant, default=list)
    evidence_count: Mapped[int] = mapped_column(Integer, default=1, server_default="1")

    # Set for context_event / commitment; NULL means permanent.
    expires_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_seen_at: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Correction/merge chain: points at the memory that replaced this one.
    superseded_by: Mapped[int | None] = mapped_column(
        ForeignKey("user_memories.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
