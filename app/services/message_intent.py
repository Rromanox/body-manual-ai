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
