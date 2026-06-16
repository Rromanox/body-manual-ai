from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class SupplementLog(Base):
    """One row per user per local day per supplement. Created by the noon
    reminder if the user hasn't already logged it that day; `taken` flips
    to True via /creatine or the reminder's inline button."""

    __tablename__ = "supplement_logs"
    __table_args__ = (
        UniqueConstraint("user_id", "name", "date", name="uq_supplement_logs_user_name_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64), default="creatine", server_default="creatine")
    date: Mapped[date] = mapped_column(Date, index=True)
    taken: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    taken_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    noon_reminder_sent: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    evening_reminder_sent: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
