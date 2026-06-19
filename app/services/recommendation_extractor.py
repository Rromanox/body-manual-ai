"""Recommendation extraction orchestration (Phase 3B).

ai_client.extract_recommendations (ModelRoute.EXTRACT) turns a generated coach
message into candidate recommendations; this module validates them
deterministically, sets the checkpoint date (backend decides timing, never the
AI), and stores keepers via recommendation_ledger. Runs as a crash-proof
background task — never blocks or breaks the user's reply.
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.user import User
from app.services import ai_client, recommendation_ledger
from app.services.recommendation_ledger import VALID_REC_TYPES
from app.services.recommendation_checkpoint import _METRIC_ALIASES
from app.services.timekit import get_user_now, now_block

logger = logging.getLogger(__name__)

MAX_STORE_PER_MESSAGE = 3

# Days from the recommendation date to the checkpoint, by source.
_CHECKPOINT_OFFSET_DAYS = {"weekly": 7}
_DEFAULT_CHECKPOINT_OFFSET = 1

# Backend backstop for the "no generic filler" rule (the prompt forbids it too).
_GENERIC_RE = re.compile(
    r"\b(stay hydrated|drink water|hydrate|listen to your body|get some rest|rest up|"
    r"rest and recover|take it easy|be well|feel better|let me know|stay safe|"
    r"prioritize sleep|prioritise sleep|get good sleep|take care)\b",
    re.IGNORECASE,
)

# Observation/explanation-only openers — not actions, so not recommendations.
_EXPLANATION_RE = re.compile(
    r"^(your |you're |you are |this is |that's |that is |it's |it is |recovery is |"
    r"hrv is |strain is |sleep is |i (see|notice|think))",
    re.IGNORECASE,
)


def _normalize_metric(value: Any) -> str | None:
    if not value:
        return None
    return _METRIC_ALIASES.get(str(value).lower())  # canonical key, or None if unknown


# Backend extraction of structured targets/flags so WHOOP follow-through inference
# works even when the model didn't populate trigger_data perfectly (Phase 3D §5).
_STRAIN_LIMIT_RE = re.compile(r"strain\s*(?:under|below|less than|<|<=)\s*(\d+(?:\.\d+)?)|(?:under|below|<|<=)\s*(\d+(?:\.\d+)?)\s*strain", re.IGNORECASE)
_TARGET_HOURS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:\+)?\s*hours?\s*(?:of\s*)?sleep|sleep\s*(?:for\s*)?(\d+(?:\.\d+)?)\s*\+?\s*hours?", re.IGNORECASE)
_EASY_DAY_RE = re.compile(r"\b(easy day|keep it easy|take it easy|light day|easy movement|recovery day|go easy|rest day)\b", re.IGNORECASE)
_AVOID_WORKOUT_RE = re.compile(r"\b(skip (?:the )?(?:training|workout|gym|session)|don'?t train|do not train|no (?:hard )?(?:training|workout)|avoid (?:training|the gym|a workout))\b", re.IGNORECASE)
_AVOID_LATE_MEAL_RE = re.compile(r"\b(avoid (?:a )?late (?:heavy )?(?:meal|dinner)|no late (?:meal|dinner)|finish dinner before|early dinner|eat earlier)\b", re.IGNORECASE)


def _normalize_trigger_data(
    trigger_data: dict[str, Any], title: str, text: str, rec_type: str
) -> dict[str, Any]:
    """Merge the model's trigger_data with backend-parsed targets/flags.

    Coerces known numeric targets and sets boolean flags (avoid_workout, easy_day,
    avoid_late_meal) the checkpoint inference relies on. The model's explicit
    values win over backend guesses.
    """
    td: dict[str, Any] = dict(trigger_data) if isinstance(trigger_data, dict) else {}
    blob = f"{title} {text}"

    # Numeric targets — coerce if present, else try to parse from text.
    for key in ("strain_limit", "target_hours"):
        if key in td:
            n = _num(td[key])
            if n is not None:
                td[key] = n
            else:
                td.pop(key, None)
    if "strain_limit" not in td:
        m = _STRAIN_LIMIT_RE.search(blob)
        if m:
            td["strain_limit"] = float(next(g for g in m.groups() if g))
    if "target_hours" not in td:
        m = _TARGET_HOURS_RE.search(blob)
        if m:
            td["target_hours"] = float(next(g for g in m.groups() if g))

    # Boolean flags (training only for workout flags). Model value wins if set.
    if rec_type == "training":
        if "easy_day" not in td and _EASY_DAY_RE.search(blob):
            td["easy_day"] = True
        if "avoid_workout" not in td and _AVOID_WORKOUT_RE.search(blob):
            td["avoid_workout"] = True
    if "avoid_late_meal" not in td and _AVOID_LATE_MEAL_RE.search(blob):
        td["avoid_late_meal"] = True

    return td


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _checkpoint_date(metric: str | None, source_type: str, local_date: date) -> date | None:
    if not metric:
        return None
    offset = _CHECKPOINT_OFFSET_DAYS.get(source_type, _DEFAULT_CHECKPOINT_OFFSET)
    return local_date + timedelta(days=offset)


def _evaluate_candidate(c: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    """Deterministic gate + normalization. Returns (keep, reason, normalized_fields)."""
    if not isinstance(c, dict):
        return False, "not_a_dict", {}
    if c.get("should_store") is False:
        return False, "should_store_false", {}
    title = str(c.get("title") or "").strip()
    text = str(c.get("recommendation_text") or "").strip()
    if not title or not text:
        return False, "empty_title_or_text", {}
    # Questions aren't recommendations.
    if title.endswith("?") or text.endswith("?"):
        return False, "question", {}
    has_hook = bool(c.get("checkpoint_metric")) or bool(c.get("trigger_data"))
    # Generic filler -> skip; in the text it's only filler when there's no
    # measurable hook (e.g. "rest and recover" alone vs "rest, keep strain <8").
    if _GENERIC_RE.search(title) or (_GENERIC_RE.search(text) and not has_hook):
        return False, "generic_advice", {}
    # Observation/explanation with no actionable hook -> skip ("your recovery is low").
    if _EXPLANATION_RE.match(title) and not has_hook:
        return False, "explanation_only", {}

    rec_type = str(c.get("recommendation_type") or "general").lower()
    if rec_type not in VALID_REC_TYPES:
        rec_type = "general"
    confidence = str(c.get("confidence") or "medium").lower()
    if confidence not in {"low", "medium", "high"}:
        confidence = "medium"
    metric = _normalize_metric(c.get("checkpoint_metric"))
    tags = [str(t) for t in (c.get("tags") or []) if str(t).strip()]
    raw_trigger = c.get("trigger_data") if isinstance(c.get("trigger_data"), dict) else {}
    trigger_data = _normalize_trigger_data(raw_trigger, title, text, rec_type)

    return True, "ok", {
        "recommendation_type": rec_type,
        "title": title[:200],
        "recommendation_text": text,
        "reason": (str(c["reason"]).strip() if c.get("reason") else None),
        "expected_outcome": (str(c["expected_outcome"]).strip() if c.get("expected_outcome") else None),
        "checkpoint_metric": metric,
        "confidence": confidence,
        "tags": tags,
        "trigger_data": trigger_data,
    }


def validate_and_store(
    session: Session,
    user_id: int,
    candidates: list[dict[str, Any]],
    *,
    source_type: str,
    local_date: date,
    source_message_id: int | None = None,
    max_store: int = MAX_STORE_PER_MESSAGE,
) -> dict[str, Any]:
    """Validate candidates and persist up to ``max_store`` new recommendations.

    Deterministic — no AI, no network. Idempotent on source_message_id, and
    dedups on a signature (type + metric + target, or normalized title) so AI
    title drift doesn't create duplicates. Returns a counts summary.
    """
    # Idempotency: if we already extracted from this exact message, do nothing.
    if source_message_id is not None and recommendation_ledger.exists_for_source_message(
        session, user_id, source_message_id
    ):
        logger.info(
            "recommendation_extract user=%s action=skipped reason=already_processed source_message_id=%s",
            user_id, source_message_id,
        )
        return {"stored": 0, "merged": 0, "skipped": 0, "already_processed": True}

    stored = merged = skipped = 0
    for c in candidates or []:
        if stored >= max_store:
            skipped += 1
            continue
        keep, reason, fields = _evaluate_candidate(c)
        snippet = str(c.get("title") or "")[:60] if isinstance(c, dict) else ""
        if not keep:
            skipped += 1
            logger.info("recommendation_extract user=%s action=skipped reason=%s title=%r", user_id, reason, snippet)
            continue

        checkpoint_date = _checkpoint_date(fields["checkpoint_metric"], source_type, local_date)
        signature = recommendation_ledger.dedup_signature(
            fields["recommendation_type"], fields["checkpoint_metric"],
            fields["trigger_data"], fields["title"], fields["recommendation_text"],
        )
        was_dup = recommendation_ledger.find_duplicate(
            session, user_id, local_date, signature
        ) is not None
        recommendation_ledger.create_recommendation(
            session, user_id,
            source_type=source_type,
            local_date=local_date,
            source_message_id=source_message_id,
            checkpoint_date=checkpoint_date,
            dedupe=True,
            commit=False,
            **fields,
        )
        if was_dup:
            merged += 1
            logger.info("recommendation_extract user=%s action=merged title=%r", user_id, snippet)
        else:
            stored += 1
            logger.info(
                "recommendation_extract user=%s action=stored type=%s metric=%s checkpoint=%s title=%r",
                user_id, fields["recommendation_type"], fields["checkpoint_metric"], checkpoint_date, snippet,
            )

    session.commit()
    summary = {"stored": stored, "merged": merged, "skipped": skipped}
    logger.info("recommendation_extract user=%s summary %s source=%s", user_id, summary, source_type)
    return summary


async def run_for_message(
    user_id: int,
    coach_message: str,
    *,
    source_type: str,
    source_message_id: int | None = None,
) -> dict[str, Any]:
    """Full background path: extract from a coach message, then store keepers.

    Crash-proof — returns {} on any failure. Safe to fire-and-forget.
    """
    try:
        with SessionLocal() as session:
            user = session.get(User, user_id)
            if user is None:
                return {}
            # Idempotency before spending an API call: skip if already processed.
            if source_message_id is not None and recommendation_ledger.exists_for_source_message(
                session, user_id, source_message_id
            ):
                logger.info(
                    "recommendation_extract user=%s action=skipped reason=already_processed source_message_id=%s",
                    user_id, source_message_id,
                )
                return {"stored": 0, "merged": 0, "skipped": 0, "already_processed": True}
            now = get_user_now(user)
            now_b = now_block(user, now)
            local_date = now.date()

        candidates = await ai_client.extract_recommendations(
            coach_message, source_type, now_b, user_id=user_id
        )
        if not candidates:
            logger.info("recommendation_extract user=%s no candidates source=%s", user_id, source_type)
            return {"stored": 0, "merged": 0, "skipped": 0}

        with SessionLocal() as session:
            return validate_and_store(
                session, user_id, candidates,
                source_type=source_type, local_date=local_date,
                source_message_id=source_message_id,
            )
    except Exception:
        logger.exception("recommendation extraction failed for user %s", user_id)
        return {}
