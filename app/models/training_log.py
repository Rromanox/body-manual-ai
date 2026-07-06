from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import Date, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, JSONVariant


class TrainingLog(Base):
    """Append-only audit of every training-plan mutation.

    One row per mutation (seed / completion / skip / move / edit / substitution /
    gate recommendation + response). ``detail`` carries the structured context —
    including which skip/reschedule rule fired — so the plan's history is fully
    reconstructable. Never updated in place.
    """

    __tablename__ = "training_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    session_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(32))   # seeded|completed|skipped|moved|edited|substituted|gate_recommendation|gate_accepted|gate_overridden
    detail: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict)
    source: Mapped[str] = mapped_column(String(16))   # command | natural_language | system
