from __future__ import annotations

from datetime import datetime

from typing import Any

from sqlalchemy import BigInteger, DateTime, Float, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    first_name: Mapped[str | None] = mapped_column(String(128))
    username: Mapped[str | None] = mapped_column(String(128))
    timezone: Mapped[str] = mapped_column(String(64))
    goal: Mapped[str | None] = mapped_column(String(64))
    max_heart_rate: Mapped[float | None] = mapped_column(Float)
    height_meter: Mapped[float | None] = mapped_column(Float)
    goal_weight_lbs: Mapped[float | None] = mapped_column(Float, nullable=True)
    coach_notes: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
