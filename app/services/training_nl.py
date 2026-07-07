"""Deterministic natural-language detection for the training plan.

Maps free-text phrases ("I skipped yesterday", "move Saturday's ride to Sunday",
"make tomorrow easier", "only have 30 minutes", "done, felt great") to the same
intents the /skip //move //edit //cant //done commands use — so both paths call
the identical shared service ops and can't diverge. Regex only; no AI needed for
these clear patterns (same approach as health_reminder.detect_reta_message).
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models.training_session import TrainingSession
from app.services import training_format as fmt
from app.services import training_plan as tp

_TRAIN_CTX = re.compile(
    r"\b(ride|rides|session|workout|training|train|gym|z2|interval|intervals|tempo|"
    r"spin|long ?ride|sweet ?spot|bike)\b", re.IGNORECASE
)

_WEEKDAY_RE = re.compile(
    r"\b(mon(?:day)?|tue(?:s|sday)?|wed(?:nesday)?|thu(?:r|rs|rsday)?|fri(?:day)?|"
    r"sat(?:urday)?|sun(?:day)?)\b", re.IGNORECASE
)
_MONTH_DAY_RE = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\b", re.IGNORECASE)
_DAY_MONTH_RE = re.compile(r"\b(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b", re.IGNORECASE)
_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _scan_date(text: str, today: date) -> date | None:
    """Find the first date-ish token in a fragment and resolve it against today."""
    low = text.lower()
    m = _ISO_RE.search(low)
    if m:
        return fmt.parse_plan_date(m.group(0), today)
    m = _MONTH_DAY_RE.search(low)
    if m:
        return fmt.parse_plan_date(f"{m.group(1)} {m.group(2)}", today)
    m = _DAY_MONTH_RE.search(low)
    if m:
        return fmt.parse_plan_date(f"{m.group(1)} {m.group(2)}", today)
    for word in ("today", "tomorrow", "yesterday"):
        if re.search(rf"\b{word}\b", low):
            return fmt.parse_plan_date(word, today)
    m = _WEEKDAY_RE.search(low)
    if m:
        return fmt.parse_plan_date(m.group(1), today)
    return None


def _snap_minutes(n: int) -> int:
    return min((30, 45, 60), key=lambda opt: abs(opt - n))


def _detect_constraint(low: str) -> dict | None:
    m = re.search(r"(\d+)\s*min", low)
    if m and re.search(r"\b(only|just|got|have|left|short on)\b", low):
        return {"constraint": "less_time", "minutes": _snap_minutes(int(m.group(1)))}
    if re.search(r"\bno bike\b|without (?:my |the )?bike|bike'?s? (?:in|at) the shop|can'?t ride|bike is broken", low):
        return {"constraint": "no_bike", "minutes": None}
    if re.search(r"can'?t leave|stuck (?:at|in|behind)|front desk|travel(?:l)?ing|on the road|at work all day", low):
        return {"constraint": "cant_leave", "minutes": None}
    if re.search(r"feeling beat|beat today|wiped|exhausted|so tired|smoked|cooked|no energy|running on empty", low):
        return {"constraint": "feeling_beat", "minutes": None}
    if re.search(r"\bcan'?t (?:train|do (?:it|this|today)|today)\b|\bcannot train\b|\bskip today\b", low):
        return {"constraint": "prompt", "minutes": None}
    return None


def detect_training_message(message: str, today: date) -> dict | None:
    """Return a training intent dict, or None when the message isn't clearly about
    the plan (so Q&A / other logging are untouched). Questions are ignored."""
    msg = (message or "").strip()
    if not msg or msg.endswith("?"):
        return None
    low = msg.lower()

    # MOVE — "move Saturday's ride to Sunday"
    if re.search(r"\bmove\b", low):
        idx = low.find(" to ")
        if idx != -1:
            from_d = _scan_date(low[:idx], today)
            to_d = _scan_date(low[idx + 4:], today)
            if from_d and to_d:
                return {"action": "move", "from": from_d, "to": to_d}

    # SKIP — "I skipped yesterday", "missed Saturday's ride"
    if re.search(r"\b(skip(?:ped|ping)?|missed|couldn'?t (?:do|make|ride))\b", low):
        if _TRAIN_CTX.search(low) or _scan_date(low, today) or len(low) <= 25:
            return {"action": "skip", "date": _scan_date(low, today) or today}

    # SOFTEN — "make tomorrow easier"
    if re.search(r"\b(easier|take it easy|go easy|ease off|dial (?:it )?back)\b", low):
        if _TRAIN_CTX.search(low) or _scan_date(low, today) or "make" in low:
            return {"action": "soften", "date": _scan_date(low, today) or today}

    # CAN'T / constraint — routes to the substitution engine
    cant = _detect_constraint(low)
    if cant:
        return {"action": "cant", **cant}

    # COMPLETE (explicit) — "did my ride", "finished today's session"
    if re.search(r"\b(did|finished|completed|crushed|nailed|got (?:it|the ride) done)\b", low) and _TRAIN_CTX.search(low):
        return {"action": "complete", "date": _scan_date(low, today) or today}

    return None


# --- bare "done" after a session was presented (mirrors reta) ----------------

_BARE_DONE_RE = re.compile(
    r"^(?:(?:done|did it|finished|completed|all done|nailed it|crushed it|"
    r"got it done|done and dusted)\b|✅|👍)",
    re.IGNORECASE,
)


def is_bare_done(message: str) -> bool:
    msg = (message or "").strip()
    if not msg or len(msg) > 30:
        return False
    return bool(_BARE_DONE_RE.match(msg))


def awaiting_done_confirmation(
    session: Session, user_id: int, now: datetime, *, within_hours: int = 6
) -> TrainingSession | None:
    """Today's session if it was presented within the window and is still pending
    — lets a bare "done" complete it without hijacking unrelated messages."""
    row = tp.get_session(session, user_id, now.date())
    if row is None or row.session_type == "rest" or row.status != "pending" or row.presented_at is None:
        return None
    last = row.presented_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    if now - last <= timedelta(hours=within_hours):
        return row
    return None
