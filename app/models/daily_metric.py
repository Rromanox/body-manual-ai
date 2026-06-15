from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class DailyMetric(Base):
    """One row per user per local date-of-waking. Wide and nullable — never fail
    when a provider is missing data."""

    __tablename__ = "daily_metrics"
    __table_args__ = (UniqueConstraint("user_id", "date", name="uq_daily_metrics_user_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)

    recovery_score: Mapped[float | None] = mapped_column(Float)
    hrv_ms: Mapped[float | None] = mapped_column(Float)
    resting_heart_rate: Mapped[float | None] = mapped_column(Float)
    respiratory_rate: Mapped[float | None] = mapped_column(Float)
    spo2: Mapped[float | None] = mapped_column(Float)
    skin_temp: Mapped[float | None] = mapped_column(Float)
    sleep_start_local: Mapped[str | None] = mapped_column(String(8))   # "HH:MM" local time
    sleep_end_local: Mapped[str | None] = mapped_column(String(8))     # "HH:MM" local time
    sleep_hours: Mapped[float | None] = mapped_column(Float)
    sleep_efficiency: Mapped[float | None] = mapped_column(Float)
    sleep_performance: Mapped[float | None] = mapped_column(Float)
    sleep_consistency: Mapped[float | None] = mapped_column(Float)
    rem_sleep_hours: Mapped[float | None] = mapped_column(Float)
    deep_sleep_hours: Mapped[float | None] = mapped_column(Float)
    light_sleep_hours: Mapped[float | None] = mapped_column(Float)
    strain: Mapped[float | None] = mapped_column(Float)
    workout_count: Mapped[int | None] = mapped_column(Integer)
    total_workout_minutes: Mapped[float | None] = mapped_column(Float)

    weight: Mapped[float | None] = mapped_column(Float)
    body_fat_pct: Mapped[float | None] = mapped_column(Float)
    muscle_mass: Mapped[float | None] = mapped_column(Float)
    fat_free_mass: Mapped[float | None] = mapped_column(Float)
    water_pct: Mapped[float | None] = mapped_column(Float)
    bone_mass: Mapped[float | None] = mapped_column(Float)
    bmi: Mapped[float | None] = mapped_column(Float)

    raw_whoop_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    raw_withings_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
