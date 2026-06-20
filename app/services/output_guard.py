"""Guard against broken / unresolved placeholder text reaching the user.

The model once sent "...reach 190 lbs in about time." — an unresolved template.
This catches that class of failure so the caller can regenerate or fall back to a
deterministic answer.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

# Unresolved placeholders / broken fragments that should never reach Telegram.
_PLACEHOLDER_PATTERNS = [
    re.compile(r"\bin about time\b", re.IGNORECASE),
    re.compile(r"\bin\s+(?:approximately|about|around)\s+time\b", re.IGNORECASE),
    re.compile(r"\{\{?.*?\}?\}"),          # {date}, {{x}}
    re.compile(r"\bN/?A\s+weeks?\b", re.IGNORECASE),
    re.compile(r"\bTBD\b"),
    re.compile(r"<[a-z_]+>", re.IGNORECASE),  # <date>, <name>
    re.compile(r"\b(None|null|undefined|NaN)\b"),
]


def has_unresolved_placeholder(text: str) -> bool:
    if not text:
        return False
    return any(p.search(text) for p in _PLACEHOLDER_PATTERNS)


# --- projection date validation ---------------------------------------------

# Tolerance: the backend date is exact; allow +/- this for rounding/phrasing.
# 2 days accepts "Jun 30 / Jul 1" but rejects "Jun 26" (4 off) and "Jul 3" (3 off).
_DATE_TOLERANCE_DAYS = 2

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_MONTH_DAY_RE = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b")
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def _extract_dates(text: str, ref_year: int) -> list[date]:
    out: list[date] = []
    for mm, dd in _MONTH_DAY_RE.findall(text or ""):
        month = _MONTHS.get(mm.lower())
        if month is None:
            continue
        try:
            out.append(date(ref_year, month, int(dd)))
        except ValueError:
            continue
    for y, m, d in _ISO_DATE_RE.findall(text or ""):
        try:
            out.append(date(int(y), int(m), int(d)))
        except ValueError:
            continue
    return out


def projection_date_is_consistent(text: str, projection: dict[str, Any] | None) -> bool:
    """False when the text states a target date that disagrees with the backend's
    estimated_date. Only applies to a "projected" projection; otherwise True
    (nothing to validate). A message that states no date is always consistent."""
    if not projection or projection.get("status") != "projected":
        return True
    est = projection.get("estimated_date")
    if not est:
        return True
    try:
        est_date = date.fromisoformat(est)
    except (ValueError, TypeError):
        return True
    mentioned = _extract_dates(_strip_markdown(text), est_date.year)
    if not mentioned:
        return True  # stated weeks but no date -> can't be wrong about a date
    # Consistent if ANY mentioned date is within tolerance of the backend date.
    return any(abs((d - est_date).days) <= _DATE_TOLERANCE_DAYS for d in mentioned)


# --- weight date/value validation -------------------------------------------

_WEIGHT_TOLERANCE_LBS = 0.3
# Catches: "June 17: 202.4 lbs", "June 17, 2026: 202.4 lbs", "Jun 17 - 200.4 lb",
# "June 17, 2026 — 202.4 pounds". Markdown (*, _, `) is stripped before matching.
_DATE_WEIGHT_RE = re.compile(
    r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*\d{4})?\s*[:\-–—]?\s*(\d{2,3}(?:\.\d)?)\s*(?:lbs?|pounds?)\b",
    re.IGNORECASE,
)
_MARKDOWN_RE = re.compile(r"[*_`]+")


def _strip_markdown(text: str) -> str:
    return _MARKDOWN_RE.sub("", text or "")


def weight_data_is_consistent(text: str, audit: dict[str, Any] | None) -> bool:
    """False when the text states a date->weight pair that contradicts a known
    stored reading (e.g. "June 17: 202.4" when June 17 was 200.4). Only checks
    dates we actually have, so legitimate mentions of other dates don't trip it."""
    if not audit:
        return True
    known: dict[str, float] = audit.get("known_weights") or {}
    if not known:
        return True
    ref_year = 2000
    cur = audit.get("current_date")
    if cur:
        try:
            ref_year = date.fromisoformat(cur).year
        except (ValueError, TypeError):
            pass
    for mon, day, weight in _DATE_WEIGHT_RE.findall(_strip_markdown(text)):
        month = _MONTHS.get(mon.lower())
        if month is None:
            continue
        try:
            iso = str(date(ref_year, month, int(day)))
        except ValueError:
            continue
        if iso in known and abs(float(weight) - known[iso]) > _WEIGHT_TOLERANCE_LBS:
            return False
    return True
