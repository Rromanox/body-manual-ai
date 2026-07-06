from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, JSONVariant


class JobRun(Base):
    """One row per scheduled-job execution — the answer to "when did X fire".

    Written by the job_log wrapper around each APScheduler job (Fix #2). Lets us
    audit reminder/pull/message runs without digging through raw logs.
    """

    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_name: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16))  # success | error
    detail: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
