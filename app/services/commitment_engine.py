"""Commitment tracking: surface what the user said they'd do this week.

Commitments are stored as events with event_type="commitment". The coach
references them in the morning message when today's data connects to them —
not as reminders, but as a coach noticing follow-through.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.event import Event


def get_active_commitments(session: Session, user_id: int, as_of_date: date) -> list[dict[str, Any]]:
    """Return commitments logged in the last 7 days, newest first (max 2)."""
    cutoff = as_of_date - timedelta(days=7)
    rows = session.scalars(
        select(Event)
        .where(
            Event.user_id == user_id,
            Event.event_type == "commitment",
            Event.local_date >= cutoff,
            Event.local_date <= as_of_date,
        )
        .order_by(Event.occurred_at.desc())
        .limit(2)
    ).all()
    return [
        {
            "text": (r.structured or {}).get("commitment_text") or r.raw_text,
            "days_ago": (as_of_date - r.local_date).days,
        }
        for r in rows
    ]
