from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class TrainingSession(Base):
    """One row per calendar day of the 12-week training plan (rest days included,
    so /week can render a full week).

    Choices (phase / session_type / priority / status) are plain strings validated
    in the service layer (app.services.training_plan) — same convention as
    health_reminders.reminder_type and events.event_type. ``user_id`` is present
    for parity with the rest of the schema and so /delete's cascade wipes the plan
    (the product spec's field list omitted it; added deliberately).
    """

    __tablename__ = "training_sessions"
    __table_args__ = (
        UniqueConstraint("user_id", "date", name="uq_training_sessions_user_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    week: Mapped[int] = mapped_column(Integer)
    phase: Mapped[str] = mapped_column(String(16))          # base | build | specificity | taper
    session_type: Mapped[str] = mapped_column(String(16))   # intervals | z2 | tempo | gym_a | gym_b | long_ride | rest
    title: Mapped[str] = mapped_column(String(200))
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    loaded: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    priority: Mapped[str] = mapped_column(String(8), default="normal", server_default="normal")
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    moved_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    completed_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    recovery_adjustment: Mapped[str | None] = mapped_column(Text, nullable=True)
    # When today's session was last shown in the morning block — lets a bare "done"
    # reply shortly after count as completing it (mirrors reta last_reminded_at).
    presented_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
