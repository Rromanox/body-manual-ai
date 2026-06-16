"""Single source of truth for the user's LOCAL time.

The bot has no persistent clock — local time is computed fresh on every
interaction from the user's IANA timezone (``users.timezone``). Anything that
needs "now" in the user's frame — payload builders, scheduler gating, event
logging — must go through these helpers. Never call ``datetime.now()`` /
``datetime.utcnow()`` directly in business logic: those return server/UTC time.

The only legitimate UTC users are absolute instants that are timezone-
independent by nature: OAuth token expiry, and the time windows handed to the
WHOOP / Withings APIs. Those stay in UTC on purpose and do not belong here.

Timezones are stored as IANA *names* (e.g. ``America/Detroit``), never fixed
offsets, so ``zoneinfo`` handles DST transitions automatically: the same zone
yields -05:00 in January (EST) and -04:00 in July (EDT).
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.models.user import User

# Default for new users until they set their own. Orlando FL is US Eastern;
# America/Detroit is the same Eastern zone (identical offset + DST rules as
# America/New_York) and is the project's chosen default.
DEFAULT_TZ = "America/Detroit"


def user_tz(user: User) -> ZoneInfo:
    """The user's timezone, falling back to the default if unset/blank."""
    return ZoneInfo(user.timezone or DEFAULT_TZ)


def get_user_now(user: User) -> datetime:
    """Current timezone-aware datetime in the user's local timezone.

    This is THE clock. Read it once per interaction and thread the value through
    so every computed field reflects the same instant.
    """
    return datetime.now(user_tz(user))


def get_user_today(user: User) -> date:
    """The user's current local calendar date."""
    return get_user_now(user).date()


def part_of_day(dt: datetime) -> str:
    """Backend-decided coarse time-of-day bucket. The AI never computes this."""
    h = dt.hour
    if 5 <= h < 12:
        return "morning"
    if 12 <= h < 17:
        return "afternoon"
    if 17 <= h < 21:
        return "evening"
    return "night"


def _twelve_hour(dt: datetime) -> str:
    # Portable AM/PM formatting — Windows strftime has no %-I (no-leading-zero).
    hour12 = dt.hour % 12 or 12
    suffix = "AM" if dt.hour < 12 else "PM"
    return f"{hour12}:{dt.minute:02d} {suffix}"


def now_block(user: User, now: datetime | None = None) -> dict[str, Any]:
    """The clean ``now`` block handed to the AI on every call.

    Every field is pre-computed by the backend; the AI only reads them and never
    derives time itself. ``now`` may be passed in to reuse a single clock read.
    """
    now = now or get_user_now(user)
    return {
        "local_datetime": now.isoformat(timespec="seconds"),
        "date": now.date().isoformat(),
        "day_of_week": now.strftime("%A"),
        "local_time": _twelve_hour(now),
        "part_of_day": part_of_day(now),
        "is_weekend": now.weekday() >= 5,
    }


# --- Relative-time anchoring (for free-text event logging) -------------------
# When the user logs an event ("had pizza", "drank at 9pm", "rough night last
# night"), the time it happened must be resolved against the user's *current*
# local now — never the server clock. This is the anchor a future event parser
# feeds raw time phrases into.

_PART_DEFAULT_HOUR = {
    "this morning": 8,
    "this afternoon": 14,
    "this evening": 18,
    "tonight": 21,
    "last night": 21,
}


def resolve_local_time(expr: str, now: datetime) -> datetime | None:
    """Resolve a time phrase to a tz-aware datetime anchored on ``now``.

    ``now`` must be tz-aware (use :func:`get_user_now`); the result is in the
    same timezone. Returns ``None`` when nothing recognizable is found, so the
    caller can fall back to ``now`` (an event with no stated time happened now).

    Handled:
      - "" / "now"                  -> now
      - "9pm" / "9:30 pm" / "21:00" -> that clock time today; if still in the
                                       future it means yesterday (at 1am, "9pm"
                                       is last night, not tonight)
      - "last night"                -> yesterday ~21:00
      - "this morning/afternoon/evening", "tonight"
      - "yesterday" (alone, or combined with a clock time)
    """
    tz = now.tzinfo
    text = (expr or "").strip().lower()
    if not text or text in ("now", "right now", "just now"):
        return now

    def at(d: date, hour: int, minute: int = 0) -> datetime:
        return datetime(d.year, d.month, d.day, hour, minute, tzinfo=tz)

    for phrase, hour in _PART_DEFAULT_HOUR.items():
        if phrase in text:
            day = now.date() - timedelta(days=1) if "last night" in phrase else now.date()
            cand = at(day, hour)
            # "this morning" at 6am hasn't reached 8am yet — clamp to now
            return min(cand, now) if cand > now and "last" not in phrase and "tonight" not in phrase else cand

    yesterday = "yesterday" in text

    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        ampm = m.group(3)
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            day = now.date() - timedelta(days=1) if yesterday else now.date()
            cand = at(day, hour, minute)
            # A bare clock time in the future means the previous day.
            if not yesterday and cand > now:
                cand -= timedelta(days=1)
            return cand

    if yesterday:
        return now - timedelta(days=1)

    return None
