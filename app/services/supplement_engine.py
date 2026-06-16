"""Creatine-taking tracking and reminder state (user-requested, not in SPEC).

One row per user per local day. /creatine and the reminder's inline button
both call mark_taken(); the reminder job reads the noon/evening flags to
decide whether it's already nudged for that slot today.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.supplement_log import SupplementLog

SUPPLEMENT_NAME = "creatine"


def get_today_log(session: Session, user_id: int, target_date: date) -> SupplementLog | None:
    return session.scalar(
        select(SupplementLog).where(
            SupplementLog.user_id == user_id,
            SupplementLog.name == SUPPLEMENT_NAME,
            SupplementLog.date == target_date,
        )
    )


def mark_taken(session: Session, user_id: int, target_date: date) -> SupplementLog:
    log = get_today_log(session, user_id, target_date)
    if log is None:
        log = SupplementLog(user_id=user_id, name=SUPPLEMENT_NAME, date=target_date)
        session.add(log)
    log.taken = True
    log.taken_at = datetime.now(timezone.utc)
    session.commit()
    return log
