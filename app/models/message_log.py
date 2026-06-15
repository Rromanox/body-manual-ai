from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class MessageLog(Base):
    """Full chat log: every message in both directions for debugging and review."""
    __tablename__ = "message_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    # nullable — we may log before a User row exists
    user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    # "in" = user → bot, "out" = bot → user
    direction: Mapped[str] = mapped_column(String(3))
    # command | q_and_a | ai_daily | ai_weekly | ai_focus | checkin | system | error
    message_type: Mapped[str] = mapped_column(String(32))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
