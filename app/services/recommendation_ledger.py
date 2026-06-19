"""RecommendationLedgerService: deterministic storage for coach recommendations.

Recommendation Ledger Phase 3A — foundation only. No AI, no extraction, no
checkpoint evaluation, no wiring into Q&A / morning / weekly / focus. This is the
only reader/writer of the recommendation_ledger table. Phase 3B will add
extraction (ModelRoute.EXTRACT) and deterministic checkpoint evaluation on top.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
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


# trigger_data keys that carry a concrete numeric/target hook. When one is
# present, dedup keys on the TARGET (not the prose), so AI title drift collapses:
# "Keep strain under 10" / "Stay below 10 strain" / "Limit strain to under 10"
# all share {type=training, metric=strain, strain_limit=10}.
_TARGET_KEYS = ("strain_limit", "target_hours", "target_bedtime", "target", "target_lbs", "hrv_pct_below")


def dedup_signature(
    recommendation_type: str | None,
    checkpoint_metric: str | None,
    trigger_data: dict[str, Any] | None,
    title: str,
    text: str,
) -> str:
    """Deterministic dedup key for a recommendation (Phase 3C).

    With a concrete target, two recommendations are "the same" when they share
    type + checkpoint metric + target values — robust to title/wording drift.
    Without a target, fall back to type + metric + normalized title so genuinely
    different actions stay distinct.
    """
    rec_type = (recommendation_type or "").lower()
    metric = (checkpoint_metric or "").lower()
    td = trigger_data or {}
    targets = {k: td[k] for k in _TARGET_KEYS if td.get(k) is not None}
    if targets:
        target_sig = ";".join(f"{k}={targets[k]}" for k in sorted(targets))
        return f"{rec_type}|{metric}|{target_sig}"
    return f"{rec_type}|{metric}|{_normalize(title)}"


def find_duplicate(
    session: Session,
    user_id: int,
    local_date: date,
    signature: str,
) -> RecommendationLedger | None:
    """A still-pending recommendation for this user/day whose dedup signature
    matches — or None. Used to avoid logging the same advice twice."""
    rows = session.scalars(
        select(RecommendationLedger).where(
            RecommendationLedger.user_id == user_id,
            RecommendationLedger.local_date == local_date,
            RecommendationLedger.status == "pending",
        )
    ).all()
    for r in rows:
        if dedup_signature(r.recommendation_type, r.checkpoint_metric, r.trigger_data, r.title, r.recommendation_text) == signature:
            return r
    return None


def exists_for_source_message(session: Session, user_id: int, source_message_id: int) -> bool:
    """Whether any recommendation was already extracted from this coach message.

    Idempotency for retries/replays/background re-runs — once a message is
    processed, we never extract from it again (regardless of those recs' status)."""
    return session.scalar(
        select(RecommendationLedger.id).where(
            RecommendationLedger.user_id == user_id,
            RecommendationLedger.source_message_id == source_message_id,
        )
    ) is not None


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
        signature = dedup_signature(recommendation_type, checkpoint_metric, trigger_data, title, recommendation_text)
        existing = find_duplicate(session, user_id, local_date, signature)
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


def set_followed_status(
    session: Session, rec_id: int, followed_status: str, *, commit: bool = True
) -> RecommendationLedger | None:
    """User-supplied follow-through (Phase 3C /recs controls). Checkpoint
    evaluation respects this — e.g. not_followed -> outcome isn't claimed."""
    if followed_status not in VALID_FOLLOWED:
        raise ValueError(f"Unknown followed_status: {followed_status!r}")
    rec = session.get(RecommendationLedger, rec_id)
    if rec is None:
        return None
    rec.followed_status = followed_status
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


def build_context(
    session: Session,
    user_id: int,
    today: date,
    *,
    since_days: int = 7,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Serialized recent recommendations (pending + recently checked) for payloads.

    Capped, newest-first, excludes cancelled. Old checked items fall out via the
    ``since_days`` window. Empty list when there's nothing — callers omit the
    payload key entirely in that case.
    """
    since = today - timedelta(days=since_days)
    # Pull a little extra, then drop cancelled and cap — so cancelled rows don't
    # eat into the limit.
    rows = get_recent(session, user_id, since=since, limit=limit * 3)
    kept = [r for r in rows if r.status != "cancelled"][:limit]
    return [serialize(r) for r in kept]


_STATUS_LABELS = {
    "pending": "Pending",
    "checked": "Checked",
    "inconclusive": "Inconclusive",
    "cancelled": "Cancelled",
}


def render_recommendation_list(recs: list[RecommendationLedger], header: str) -> str:
    """Plain-text Telegram rendering for /recs (Phase 3C). Pure function.

    Pending rows show what will be checked; resolved rows show the outcome. No
    Markdown — recommendation text is AI-derived and would break the parser."""
    if not recs:
        return f"{header}\n\nNothing yet — I'll log advice as I give it."
    lines = [header, ""]
    for r in recs:
        if r.status == "pending":
            lines.append(f"[{r.id}] {r.recommendation_type.title()} — {r.title}")
            meta = []
            if r.checkpoint_date:
                meta.append(f"Checkpoint: {r.checkpoint_date}")
            if r.checkpoint_metric:
                meta.append(f"Metric: {r.checkpoint_metric}")
            meta.append(f"Source: {r.source_type}")
            meta.append(f"Confidence: {r.confidence}")
            lines.append("    " + " · ".join(meta))
        elif r.status in ("checked", "inconclusive"):
            outcome = r.outcome_status if r.outcome_status not in (None, "unknown") else r.status
            lines.append(f"[{r.id}] {r.title}")
            lines.append(f"    Outcome: {outcome} · Followed: {r.followed_status}")
            if r.outcome_summary:
                lines.append(f"    {r.outcome_summary}")
        else:  # cancelled
            lines.append(f"[{r.id}] {r.title} — cancelled")
        lines.append("")
    return "\n".join(lines).rstrip()


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
