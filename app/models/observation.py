from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Observation(Base):
    __tablename__ = "observations"
    __table_args__ = (UniqueConstraint("user_id", "pattern_key", name="uq_observations_user_pattern"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    pattern_key: Mapped[str] = mapped_column(String(64))
    pattern_description: Mapped[str] = mapped_column(Text)
    trigger_tag: Mapped[str | None] = mapped_column(String(32))
    outcome_metric: Mapped[str | None] = mapped_column(String(32))
    occurrence_count: Mapped[int] = mapped_column(Integer, default=0)
    supporting_count: Mapped[int] = mapped_column(Integer, default=0)
    opposing_count: Mapped[int] = mapped_column(Integer, default=0)
    first_seen: Mapped[date | None] = mapped_column(Date)
    last_seen: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default="watching", server_default="watching")
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
