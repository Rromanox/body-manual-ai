"""RecommendationLedgerService: deterministic storage for coach recommendations.

Recommendation Ledger Phase 3A — foundation only. No AI, no extraction, no
checkpoint evaluation, no wiring into Q&A / morning / weekly / focus. This is the
only reader/writer of the recommendation_ledger table. Phase 3B will add
extraction (ModelRoute.EXTRACT) and deterministic checkpoint evaluation on top.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.recommendation import RecommendationLedger

logger = logging.getLogger(__name__)

VALID_SOURCE_TYPES: frozenset[str] = frozenset({"daily", "qa", "focus", "weekly", "manual"})
VALID_REC_TYPES: frozenset[str] = frozenset({
    "training", "sleep", "nutrition", "recovery", "weight", "behavior", "general",
})
VALID_STATUS: frozenset[str] = frozenset({"pending", "checked", "inconclusive", "cancelled"})
VALID_FOLLOWED: frozenset[str] = frozenset({"unknown", "followed", "not_followed", "partial"})
VALID_OUTCOME: frozenset[str] = frozenset({"unknown", "improved", "worsened", "neutral", "inconclusive"})
VALID_CONFIDENCE: frozenset[str] = frozenset({"low", "medium", "high"})


def _normalize(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower()).rstrip(".!?,;: ")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def find_duplicate(
    session: Session,
    user_id: int,
    local_date: date,
    recommendation_type: str,
    title: str,
) -> RecommendationLedger | None:
    """A still-pending recommendation of the same type, same day, same (normalized)
    title for this user — or None. Used to avoid logging the same advice twice."""
    norm = _normalize(title)
    rows = session.scalars(
        select(RecommendationLedger).where(
            RecommendationLedger.user_id == user_id,
            RecommendationLedger.local_date == local_date,
            RecommendationLedger.recommendation_type == recommendation_type,
            RecommendationLedger.status == "pending",
        )
    ).all()
    for r in rows:
        if _normalize(r.title) == norm:
            return r
    return None


def create_recommendation(
    session: Session,
    user_id: int,
    *,
    source_type: str,
    recommendation_type: str,
    title: str,
    recommendation_text: str,
    reason: str | None = None,
    trigger_data: dict[str, Any] | None = None,
    expected_outcome: str | None = None,
    checkpoint_metric: str | None = None,
    checkpoint_date: date | None = None,
    local_date: date | None = None,
    source_message_id: int | None = None,
    confidence: str = "medium",
    tags: list[str] | None = None,
    dedupe: bool = True,
    commit: bool = True,
) -> RecommendationLedger:
    """Create a recommendation row (or return an existing same-day duplicate).

    Deterministic — no AI. Validates enums and required fields. When ``dedupe`` is
    True and a pending same-day/same-type/same-title recommendation already
    exists, that row is returned instead of inserting a duplicate.
    """
    if source_type not in VALID_SOURCE_TYPES:
        raise ValueError(f"Unknown source_type: {source_type!r}")
    if recommendation_type not in VALID_REC_TYPES:
        raise ValueError(f"Unknown recommendation_type: {recommendation_type!r}")
    if confidence not in VALID_CONFIDENCE:
        raise ValueError(f"Unknown confidence: {confidence!r}")
    title = (title or "").strip()
    recommendation_text = (recommendation_text or "").strip()
    if not title:
        raise ValueError("Recommendation title cannot be empty")
    if not recommendation_text:
        raise ValueError("Recommendation text cannot be empty")

    local_date = local_date or date.today()

    if dedupe:
        existing = find_duplicate(session, user_id, local_date, recommendation_type, title)
        if existing is not None:
            logger.info(
                "recommendation dedup user=%s date=%s type=%s title=%r -> existing id=%s",
                user_id, local_date, recommendation_type, title[:60], existing.id,
            )
            return existing

    rec = RecommendationLedger(
        user_id=user_id,
        local_date=local_date,
        source_message_id=source_message_id,
        source_type=source_type,
        recommendation_type=recommendation_type,
        title=title,
        recommendation_text=recommendation_text,
        reason=reason,
        trigger_data=trigger_data or {},
        expected_outcome=expected_outcome,
        checkpoint_metric=checkpoint_metric,
        checkpoint_date=checkpoint_date,
        confidence=confidence,
        tags=tags or [],
        status="pending",
        followed_status="unknown",
        outcome_status="unknown",
    )
    session.add(rec)
    if commit:
        session.commit()
    else:
        session.flush()
    logger.info(
        "recommendation created user=%s date=%s type=%s checkpoint=%s/%s title=%r",
        user_id, local_date, recommendation_type, checkpoint_metric, checkpoint_date, title[:60],
    )
    return rec


def get_recommendation(session: Session, rec_id: int) -> RecommendationLedger | None:
    return session.get(RecommendationLedger, rec_id)


def get_pending(
    session: Session, user_id: int, *, limit: int | None = None
) -> list[RecommendationLedger]:
    """Pending (not yet checked/cancelled) recommendations, newest first."""
    stmt = (
        select(RecommendationLedger)
        .where(
            RecommendationLedger.user_id == user_id,
            RecommendationLedger.status == "pending",
        )
        .order_by(RecommendationLedger.id.desc())
    )
    rows = list(session.scalars(stmt).all())
    return rows[:limit] if limit is not None else rows


def get_due_checkpoints(
    session: Session, user_id: int, as_of_date: date, *, limit: int | None = None
) -> list[RecommendationLedger]:
    """Pending recommendations whose checkpoint_date has arrived (<= as_of_date).

    Oldest checkpoint first so the earliest-due gets evaluated first in 3B.
    """
    stmt = (
        select(RecommendationLedger)
        .where(
            RecommendationLedger.user_id == user_id,
            RecommendationLedger.status == "pending",
            RecommendationLedger.checkpoint_date.is_not(None),
            RecommendationLedger.checkpoint_date <= as_of_date,
        )
        .order_by(RecommendationLedger.checkpoint_date.asc(), RecommendationLedger.id.asc())
    )
    rows = list(session.scalars(stmt).all())
    return rows[:limit] if limit is not None else rows


def mark_checked(
    session: Session,
    rec_id: int,
    *,
    outcome_status: str,
    outcome_summary: str | None = None,
    followed_status: str | None = None,
    commit: bool = True,
) -> RecommendationLedger | None:
    """Resolve a recommendation with a measured outcome (deterministic in 3B)."""
    if outcome_status not in VALID_OUTCOME:
        raise ValueError(f"Unknown outcome_status: {outcome_status!r}")
    if followed_status is not None and followed_status not in VALID_FOLLOWED:
        raise ValueError(f"Unknown followed_status: {followed_status!r}")
    rec = session.get(RecommendationLedger, rec_id)
    if rec is None:
        return None
    rec.status = "checked"
    rec.outcome_status = outcome_status
    if outcome_summary is not None:
        rec.outcome_summary = outcome_summary
    if followed_status is not None:
        rec.followed_status = followed_status
    rec.checked_at = _now()
    if commit:
        session.commit()
    return rec


def mark_inconclusive(
    session: Session,
    rec_id: int,
    *,
    outcome_summary: str | None = None,
    commit: bool = True,
) -> RecommendationLedger | None:
    """Resolve a recommendation we couldn't measure (e.g. missing data)."""
    rec = session.get(RecommendationLedger, rec_id)
    if rec is None:
        return None
    rec.status = "inconclusive"
    rec.outcome_status = "inconclusive"
    if outcome_summary is not None:
        rec.outcome_summary = outcome_summary
    rec.checked_at = _now()
    if commit:
        session.commit()
    return rec


def cancel(session: Session, rec_id: int, *, commit: bool = True) -> RecommendationLedger | None:
    """Cancel a recommendation (superseded / no longer relevant)."""
    rec = session.get(RecommendationLedger, rec_id)
    if rec is None:
        return None
    rec.status = "cancelled"
    if commit:
        session.commit()
    return rec


def get_recent(
    session: Session,
    user_id: int,
    *,
    since: date | None = None,
    limit: int = 20,
) -> list[RecommendationLedger]:
    """Most recent recommendations regardless of status, newest first.

    Optional ``since`` bounds by local_date. For future AI payloads ("here's what
    I told you and whether it worked").
    """
    stmt = select(RecommendationLedger).where(RecommendationLedger.user_id == user_id)
    if since is not None:
        stmt = stmt.where(RecommendationLedger.local_date >= since)
    stmt = stmt.order_by(RecommendationLedger.id.desc())
    return list(session.scalars(stmt).all())[:limit]


def serialize(rec: RecommendationLedger) -> dict[str, Any]:
    """Compact dict for future AI payloads. Omits null/empty fields."""
    out: dict[str, Any] = {
        "id": rec.id,
        "date": str(rec.local_date),
        "type": rec.recommendation_type,
        "recommendation": rec.recommendation_text,
        "status": rec.status,
    }
    if rec.reason:
        out["reason"] = rec.reason
    if rec.expected_outcome:
        out["expected_outcome"] = rec.expected_outcome
    if rec.checkpoint_metric:
        out["checkpoint_metric"] = rec.checkpoint_metric
    if rec.checkpoint_date:
        out["checkpoint_date"] = str(rec.checkpoint_date)
    if rec.followed_status and rec.followed_status != "unknown":
        out["followed"] = rec.followed_status
    if rec.outcome_status and rec.outcome_status != "unknown":
        out["outcome"] = rec.outcome_status
    if rec.outcome_summary:
        out["outcome_summary"] = rec.outcome_summary
    if rec.tags:
        out["tags"] = list(rec.tags)
    return out
