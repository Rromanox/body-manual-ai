"""Presentation for the training plan — the strings both commands and the NL flow
render, plus date parsing for command args. No DB writes here."""
from __future__ import annotations

import re
from datetime import date, timedelta

from app.models.training_session import TrainingSession
from app.services import training_plan as tp

STATUS_ICON = {
    "completed": "✅",
    "skipped": "⏭",
    "moved": "🔁",
    "pending": "⬜",
    "modified": "✳️",
}
REST_ICON = "💤"

_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _icon(row: TrainingSession) -> str:
    if row.session_type == "rest":
        return REST_ICON
    return STATUS_ICON.get(row.status, "⬜")


def _day_label(d: date) -> str:
    return f"{d:%a %b} {d.day}"


def format_session_line(row: TrainingSession) -> str:
    dur = f" ({row.duration_min}m)" if row.duration_min else ""
    loaded = " 🎒" if row.loaded else ""
    star = "⭐ " if row.priority == "critical" else ""
    return f"{_icon(row)} {_day_label(row.date)} — {star}{row.title}{dur}{loaded}"


def format_week(rows: list[TrainingSession], week: int, *, today: date | None = None) -> str:
    phase = tp.phase_for_week(week)
    start, _ = tp.week_date_range(week)
    header = f"📋 Week {week} of {tp.PLAN_WEEKS} — {phase.title()}"
    # If this week is still in the future (e.g. the plan hasn't started), say so,
    # so "Week 1" is never mistaken for the current calendar week.
    if today is not None and start > today:
        header += f" — starts {start:%a} {start:%b} {start.day}"
    if not rows:
        return header + "\n(no sessions seeded yet)"
    return header + "\n" + "\n".join(format_session_line(r) for r in rows)


def format_plan(overview: dict, *, today: date | None = None) -> str:
    wk = overview["current_week"]
    phase = overview["current_phase"]
    if wk is None:
        if today is not None and today < tp.PLAN_START:
            days = (tp.PLAN_START - today).days
            start = tp.PLAN_START
            head = (
                f"📊 Plan — hasn't started yet. Week 1 ({tp.phase_for_week(1).title()}) begins "
                f"{start:%A}, {start:%b} {start.day} — {days} day{'s' if days != 1 else ''} to go."
            )
        else:
            head = (
                f"📊 Plan — complete (ran {tp.PLAN_START:%b} {tp.PLAN_START.day}"
                f"–{tp.PLAN_END:%b} {tp.PLAN_END.day})."
            )
        return f"{head}\n⭐ Critical rides: {overview['critical_done']} of {overview['critical_total']} done"
    return (
        f"📊 Plan — Week {wk} of {tp.PLAN_WEEKS} ({phase})\n"
        f"Completed: {overview['completed_sessions']}/{overview['total_sessions']} "
        f"sessions ({overview['completion_pct']}%)\n"
        f"⭐ Critical rides: {overview['critical_done']} of {overview['critical_total']} done "
        f"({overview['critical_remaining']} to go)"
    )


def format_today_block(row: TrainingSession | None, gate, overview: dict) -> str:
    """The check-in block (spec §Recovery gate)."""
    wk = overview["current_week"]
    phase = (overview["current_phase"] or "").title()
    header = f"🚴 TODAY — Week {wk}, {phase}" if wk else "🚴 TODAY"
    if row is None or row.session_type == "rest":
        body = f"{REST_ICON} Rest day"
    else:
        dur = f" ({row.duration_min} min)" if row.duration_min else ""
        loaded = " 🎒 loaded" if row.loaded else ""
        body = f"{row.title}{dur}{loaded}"
    lines = [header, body]
    if gate is not None:
        lines.append(gate.recovery_line)
    lines.append(f"Critical rides remaining: {overview['critical_remaining']} of {overview['critical_total']}")
    if row is not None and row.recovery_adjustment and (gate is None or not gate.adjustment):
        lines.append(f"Adjustment on file: {row.recovery_adjustment}")
    if gate is not None:
        for note in gate.notes:
            lines.append(note)
    return "\n".join(lines)


def parse_plan_date(text: str, today: date) -> date | None:
    """Parse a date phrase from a command arg. Supports today/yesterday/tomorrow,
    ISO (YYYY-MM-DD), weekday names (this plan-week's occurrence), and 'Mon DD'."""
    t = (text or "").strip().lower()
    if not t:
        return None
    if t in ("today", "tod"):
        return today
    if t in ("yesterday", "yday"):
        return today - timedelta(days=1)
    if t in ("tomorrow", "tmr", "tmrw"):
        return today + timedelta(days=1)

    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None

    if t in _WEEKDAYS:
        monday = today - timedelta(days=today.weekday())
        return monday + timedelta(days=_WEEKDAYS[t])

    m = re.fullmatch(r"([a-z]{3,9})\.?\s+(\d{1,2})", t)
    if m and m[1][:3] in _MONTHS:
        try:
            return date(today.year, _MONTHS[m[1][:3]], int(m[2]))
        except ValueError:
            return None
    m = re.fullmatch(r"(\d{1,2})\s+([a-z]{3,9})", t)
    if m and m[2][:3] in _MONTHS:
        try:
            return date(today.year, _MONTHS[m[2][:3]], int(m[1]))
        except ValueError:
            return None
    return None
