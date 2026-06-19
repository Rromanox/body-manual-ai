"""Natural-language follow-through detection for recommendations (Phase 3D).

Lets the user say things like "I skipped training" or "stayed under 10" instead
of `/recs followed 1`. High-precision deterministic patterns handle the obvious
cases for free; anything ambiguous falls back to ai_client (ModelRoute.EXTRACT),
which can also ask one clarifying question. The /recs commands remain as manual
backup. Nothing here judges the advice — that stays in recommendation_checkpoint.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy.orm import Session

from app.services import ai_client, recommendation_ledger

logger = logging.getLogger(__name__)

VALID_FOLLOWED = {"followed", "not_followed", "partial"}

# Gate: does this message look at all like a follow-through report? Cheap filter
# so we don't run the AI on every message. Questions are never follow-through.
_SIGNAL_RE = re.compile(
    r"\b(train(ed|ing)?|work(ed)?\s*out|workout|gym|skip(ped)?|rest(ed)?|"
    r"easy|walk(ed|ing)?|strain|under|over|did|didn'?t|stayed|kept|went|"
    r"session|exercis|ran|run|lifted|hard|took it easy)\b",
    re.IGNORECASE,
)

# High-precision intent patterns (past-tense self-reports only).
_TRAINED_RE = re.compile(r"\b(i (trained|worked out|did (my|the) (workout|session|training)|hit the gym)|trained anyway|went hard|did the workout)\b", re.IGNORECASE)
_SKIPPED_RE = re.compile(r"\b(didn'?t train|did not train|skipped (the )?(training|workout|gym|session)|no training|didn'?t work out|did not work out|took (the day|a rest day) off|rested today|i rested|stayed home)\b", re.IGNORECASE)
_EASY_RE = re.compile(r"\b(kept it easy|took it easy|only walked|just walked|light day|kept it light|easy movement|easy day)\b", re.IGNORECASE)
_STRAIN_UNDER_RE = re.compile(r"\b(stayed (under|below)|kept (it |strain )?(under|below)|under \d+|below \d+|kept strain low)\b", re.IGNORECASE)
_STRAIN_OVER_RE = re.compile(r"\b(went over|over \d+|above \d+|exceeded|blew past|went hard)\b", re.IGNORECASE)


def looks_like_followthrough(message: str) -> bool:
    msg = (message or "").strip()
    if not msg or msg.endswith("?"):
        return False
    return bool(_SIGNAL_RE.search(msg))


def _is_training_rec(rec) -> bool:
    td = rec.trigger_data or {}
    return bool(td.get("avoid_workout") or td.get("easy_day")) or rec.recommendation_type == "training"


def _is_strain_rec(rec) -> bool:
    return (rec.trigger_data or {}).get("strain_limit") is not None


def match_deterministic(message: str, pending: list) -> dict[str, Any] | None:
    """High-precision mapping of an obvious message to ONE pending rec.

    Returns a decision dict, or None when it's not obvious (ambiguous, no match,
    or nuanced) — the caller then falls back to the AI."""
    msg = message or ""

    # Determine the intent + which rec-type it targets + resulting status.
    if _SKIPPED_RE.search(msg):
        targets, status = [r for r in pending if _is_training_rec(r)], "followed"
    elif _TRAINED_RE.search(msg):
        targets, status = [r for r in pending if _is_training_rec(r)], "not_followed"
    elif _STRAIN_UNDER_RE.search(msg):
        targets = [r for r in pending if _is_strain_rec(r)] or [r for r in pending if _is_training_rec(r)]
        status = "followed"
    elif _STRAIN_OVER_RE.search(msg):
        targets = [r for r in pending if _is_strain_rec(r)] or [r for r in pending if _is_training_rec(r)]
        status = "not_followed"
    elif _EASY_RE.search(msg):
        # "only walked" etc. — nuance (followed vs partial) better left to AI/WHOOP.
        return None
    else:
        return None

    if len(targets) != 1:
        return None  # zero or ambiguous -> let the AI decide / clarify
    return {
        "should_update": True,
        "recommendation_id": targets[0].id,
        "followed_status": status,
        "confidence": "high",
        "reason": "deterministic pattern match",
    }


async def detect_with_ai(
    message: str, pending: list, now: dict[str, Any], user_id: int
) -> dict[str, Any]:
    """AI fallback when the deterministic layer can't decide. Returns a validated
    decision dict (should_update/recommendation_id/followed_status/...)."""
    serialized = [
        {"id": r.id, "type": r.recommendation_type, "title": r.title, "recommendation": r.recommendation_text}
        for r in pending
    ]
    decision = await ai_client.extract_recommendation_followthrough(message, serialized, now, user_id=user_id)
    if not isinstance(decision, dict):
        return {"should_update": False}
    # Validate: id must be a real pending id, status must be valid.
    valid_ids = {r.id for r in pending}
    if decision.get("should_update"):
        if decision.get("recommendation_id") not in valid_ids or decision.get("followed_status") not in VALID_FOLLOWED:
            decision["should_update"] = False
    return decision


def apply_decision(session: Session, user_id: int, decision: dict[str, Any]):
    """Persist a follow-through decision (owner-scoped). Returns the rec or None."""
    if not decision or not decision.get("should_update"):
        return None
    rec_id = decision.get("recommendation_id")
    status = decision.get("followed_status")
    if rec_id is None or status not in VALID_FOLLOWED:
        return None
    rec = recommendation_ledger.get_recommendation(session, rec_id)
    if rec is None or rec.user_id != user_id:
        return None
    recommendation_ledger.set_followed_status(session, rec_id, status)
    logger.info(
        "recommendation_followthrough user=%s rec=%s status=%s confidence=%s",
        user_id, rec_id, status, decision.get("confidence"),
    )
    return rec


def confirmation_text(status: str) -> str:
    if status == "followed":
        return "Got it — I marked that recommendation as followed."
    if status == "not_followed":
        return "Got it — I marked that one as not followed, so I won't treat the outcome as a fair test of the advice."
    if status == "partial":
        return "Got it — I marked it as partial."
    return "Got it."
