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


class RecommendationLedger(Base):
    """One piece of meaningful advice the coach gave, with why + how to check it.

    Recommendation Ledger Phase 3A: this is deterministic storage only. Nothing
    extracts recommendations from AI replies or evaluates checkpoints yet (3B);
    nothing user-facing reads or writes this table yet. Written/read solely by
    app/services/recommendation_ledger.py.
    """

    __tablename__ = "recommendation_ledger"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    local_date: Mapped[date] = mapped_column(Date, index=True)

    # Soft link to the coach_messages row this advice came from (SET NULL so a
    # deleted message doesn't drop the ledger row).
    source_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("coach_messages.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # daily | qa | focus | weekly | manual
    source_type: Mapped[str] = mapped_column(String(16))
    # training | sleep | nutrition | recovery | weight | behavior | general
    recommendation_type: Mapped[str] = mapped_column(String(24))

    title: Mapped[str] = mapped_column(String(200))
    recommendation_text: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    trigger_data: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict)
    expected_outcome: Mapped[str | None] = mapped_column(Text, nullable=True)

    checkpoint_metric: Mapped[str | None] = mapped_column(String(32), nullable=True)
    checkpoint_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # pending | checked | inconclusive | cancelled
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    # unknown | followed | not_followed | partial
    followed_status: Mapped[str] = mapped_column(String(16), default="unknown", server_default="unknown")
    # unknown | improved | worsened | neutral | inconclusive
    outcome_status: Mapped[str] = mapped_column(String(16), default="unknown", server_default="unknown")
    outcome_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    confidence: Mapped[str] = mapped_column(String(12), default="medium", server_default="medium")
    tags: Mapped[list[Any]] = mapped_column(JSONVariant, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
