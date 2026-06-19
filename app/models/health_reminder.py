from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class HealthReminder(Base):
    """A simple recurring, interval-based reminder the user set themselves.

    Currently used for the retatrutide shot ("remind me every 6 days"). This is a
    user-specified reminder only — never medical advice and never a dosage. One
    row per (user, reminder_type). The next due date is recomputed from the actual
    logged completion date, so a late log shifts the schedule forward.
    """

    __tablename__ = "health_reminders"
    __table_args__ = (
        UniqueConstraint("user_id", "reminder_type", name="uq_health_reminders_user_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    reminder_type: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(64))
    interval_days: Mapped[int] = mapped_column(Integer)
    last_completed_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    next_due_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    last_reminded_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
