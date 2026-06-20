"""Lightweight deterministic intent detectors for the free-text flow.

- is_correction: the user is challenging/correcting the bot's previous answer, so
  the message must NOT be swallowed by the log/event classifier — it should
  continue the conversation (Q&A) and recompute.
- detect_status_memory: the user is stating a current status/fact ("remember I'm
  taking retatrutide") — store it as memory, NOT as a commitment or log.
"""
from __future__ import annotations

import re

# Phrases that mean "you got that wrong / explain / redo it" — a correction or
# objection, never a thing to log.
_CORRECTION_RE = re.compile(
    r"\b("
    r"math ain'?t mathing|that('?s| is) wrong|you'?re wrong|you are wrong|"
    r"that'?s incorrect|that('?s| is)n'?t right|not right|doesn'?t make sense|"
    r"does that make sense|how (does|is) that|check (that|it) again|recalculate|"
    r"re-?calculate|recompute|do the math|try again|that'?s off|that seems off|"
    r"your (number|numbers|math|rate|estimate|projection|calculation)|"
    r"wym|what do you mean|that can'?t be right|makes no sense|wrong"
    r")\b",
    re.IGNORECASE,
)


def is_correction(message: str) -> bool:
    return bool(_CORRECTION_RE.search(message or ""))


# "remember I'm taking X" / "I'm taking X" / "I'm on X" / "I take X" — present
# status, not a future commitment. Captures the trailing phrase.
_STATUS_RE = re.compile(
    r"\b(?:remember(?:\s+that)?\s+)?i'?m\s+(?:currently\s+)?(?:taking|on|using)\s+(.+)"
    r"|\bremember(?:\s+that)?\s+i\s+take\s+(.+)"
    r"|\bi\s+take\s+(.+?)\s+(?:daily|regularly|every\b.*)",
    re.IGNORECASE,
)
# Whether the message explicitly asks to remember (lets bare "I'm taking X" through
# only when X names a known substance, to avoid "I'm taking longer to recover").
_REMEMBER_RE = re.compile(r"\bremember\b", re.IGNORECASE)
# Substances/medications worth remembering as standing context.
_SUBSTANCE_RE = re.compile(
    r"\b(reta|retatrutide|semaglutide|ozempic|wegovy|tirzepatide|mounjaro|"
    r"creatine|peptides?|glp-?1|metformin|testosterone|trt|hrt|melatonin|"
    r"ashwagandha|magnesium|finasteride|tesofensine|supplements?)\b",
    re.IGNORECASE,
)
# Future actions are commitments, not status — don't treat these as status.
_FUTURE_RE = re.compile(r"\b(will|going to|gonna|i'?ll|plan to|tomorrow|next week|on \w+day)\b", re.IGNORECASE)
_STOPWORDS = {"it", "that", "this", "them", "care", "my", "the", "a", "some", "longer"}


def detect_status_memory(message: str) -> str | None:
    """Return the status phrase when the message states a current status/fact to
    remember, else None. Skips questions and future actions. A bare "I'm taking X"
    only counts when X names a known substance OR the user said "remember"."""
    msg = (message or "").strip()
    if not msg or msg.endswith("?"):
        return None
    if _FUTURE_RE.search(msg):
        return None
    m = _STATUS_RE.search(msg)
    if not m:
        return None
    if not (_REMEMBER_RE.search(msg) or _SUBSTANCE_RE.search(msg)):
        return None  # avoids "I'm taking longer to recover", "I'm taking a break"
    phrase = next((g for g in m.groups() if g), "").strip(" .!,")
    # Trim trailing clauses after a conjunction to keep the core noun.
    phrase = re.split(r"\b(and|but|so|because)\b", phrase, maxsplit=1)[0].strip(" .!,")
    if not phrase or phrase.lower() in _STOPWORDS or len(phrase) < 2:
        return None
    return phrase


# --- constraint / preference detection --------------------------------------

# Question openers (without a trailing "?") that we must NOT treat as a statement.
_QUESTION_PREFIXES = (
    "can ", "could ", "should ", "do ", "does ", "did ", "will ", "would ",
    "is ", "are ", "when ", "what ", "how ", "why ", "where ", "who ",
)

# Hard limits (constraint) and softer habits (preference). Each matches from the
# cue to the end of the message; the cue word stays in the stored content.
_CONSTRAINT_PATTERNS = [
    r"\bi can only\b.+",
    r"\bi can'?t\b.+",
    r"\bi cannot\b.+",
    r"\bi only have\b.+",
    r"\bi (?:don'?t|do not) have\b.+\b(gym|equipment|weights|access)\b.*",
    r"\bi (?:work|am working)\s+\d[\d:apm\s]*\bto\b.+",
]
_PREFERENCE_PATTERNS = [
    r"\bi (?:usually|normally|typically|generally)\s+(?:train|work ?out|exercise|run|lift)\b.+",
    r"\bi prefer\b.+",
    r"\bi (?:train|work ?out|exercise)\s+at home\b.*",
]


def _clean_constraint(message: str, start: int) -> str:
    """Original-case text from the cue to end, with a leading 'I ' dropped."""
    frag = message[start:].strip(" .!,")
    frag = re.sub(r"^i\s+", "", frag, flags=re.IGNORECASE)
    return frag[:1].upper() + frag[1:] if frag else frag


def detect_constraint_memory(message: str) -> dict | None:
    """Detect a stable training constraint/preference. Returns
    {"type": "constraint"|"preference", "content": str} or None. Skips questions
    so "Can I train tomorrow morning?" stays a Q&A."""
    msg = (message or "").strip()
    if not msg or msg.endswith("?"):
        return None
    low = msg.lower()
    if low.startswith(_QUESTION_PREFIXES):
        return None
    for pat in _CONSTRAINT_PATTERNS:
        m = re.search(pat, low)
        if m:
            return {"type": "constraint", "content": _clean_constraint(msg, m.start())}
    for pat in _PREFERENCE_PATTERNS:
        m = re.search(pat, low)
        if m:
            return {"type": "preference", "content": _clean_constraint(msg, m.start())}
    return None
