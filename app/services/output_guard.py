"""Guard against broken / unresolved placeholder text reaching the user.

The model once sent "...reach 190 lbs in about time." — an unresolved template.
This catches that class of failure so the caller can regenerate or fall back to a
deterministic answer.
"""
from __future__ import annotations

import re

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
