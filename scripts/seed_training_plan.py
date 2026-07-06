"""Seed the 12-week bikepacking training plan (Jul 13 – Oct 4, 2026).

Idempotent: upserts by (user_id, date) via training_plan.upsert_session, so a
re-run refreshes the planned definitions without duplicating rows or wiping
progress (status / notes are preserved). Days not listed below are seeded as
rest days so /week can render a full week.

Run:  python -m scripts.seed_training_plan
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.services import training_plan as tp

logger = logging.getLogger(__name__)

# Shared strength templates (spec §Seed script).
GYM_A_DETAILS = (
    "10 min warm-up spin; goblet/back squat 4×8; RDL 3×8; Bulgarian split squat "
    "3×8/leg; step-ups 3×10/leg; plank 3×60s + side plank 3×30s/side + dead bugs "
    "3×10; farmer carries 3×40yd; dead hangs 3×30s"
)
GYM_B_DETAILS = (
    "10 min warm-up; goblet squat 3×8 moderate; single-leg RDL 3×8/leg; step-ups "
    "2×10/leg; core circuit ×2 rounds; farmer carries 2×40yd"
)


def _s(
    d: date,
    session_type: str,
    title: str,
    duration_min: int | None = None,
    *,
    priority: str = "normal",
    loaded: bool = False,
    details: str | None = None,
) -> dict[str, Any]:
    if session_type == "gym_a" and details is None:
        details = GYM_A_DETAILS
    elif session_type == "gym_b" and details is None:
        details = GYM_B_DETAILS
    return {
        "date": d,
        "session_type": session_type,
        "title": title,
        "duration_min": duration_min,
        "priority": priority,
        "loaded": loaded,
        "details": details,
    }


# Every non-rest session (and the one titled rest day, Oct 4). Week + phase are
# derived from the date, so they can't drift out of sync with the calendar.
PLAN: list[dict[str, Any]] = [
    # Week 1 — base
    _s(date(2026, 7, 14), "intervals", "3×10 min Sweet Spot", 60,
       details="15 min warm-up, 3×10 min SS w/ 5 min easy between, cool down"),
    _s(date(2026, 7, 15), "z2", "Easy Z2 spin", 60),
    _s(date(2026, 7, 17), "gym_a", "Strength A", 60),
    _s(date(2026, 7, 18), "long_ride", "90 min endurance", 90, priority="high",
       details="Upper Z2, gravel if possible. First ride on new bike — note fit issues immediately."),
    _s(date(2026, 7, 19), "z2", "Optional easy Z2", 50,
       details="45–60 min very easy. Skippable this week only."),
    # Week 2 — base
    _s(date(2026, 7, 21), "intervals", "3×10 min Sweet Spot", 70),
    _s(date(2026, 7, 22), "z2", "Easy Z2 spin", 60),
    _s(date(2026, 7, 24), "gym_a", "Strength A", 60),
    _s(date(2026, 7, 25), "long_ride", "90 min endurance + tempo hills", 90, priority="high",
       details="2–3 sustained hills or gravel climbs at tempo"),
    _s(date(2026, 7, 26), "z2", "Back-to-back Z2", 60,
       details="Not optional — back-to-back habit starts now"),
    # Week 3 — base
    _s(date(2026, 7, 28), "intervals", "3×12 min Sweet Spot", 75),
    _s(date(2026, 7, 29), "z2", "Easy Z2 spin", 60),
    _s(date(2026, 7, 31), "gym_a", "Strength A", 60),
    _s(date(2026, 8, 1), "long_ride", "90 min w/ 30 min tempo middle", 90, priority="high"),
    _s(date(2026, 8, 2), "z2", "Back-to-back Z2", 60),
    # Week 4 — base, RECOVERY (Wed + Sun rest)
    _s(date(2026, 8, 4), "intervals", "2×10 min Sweet Spot (recovery week)", 60),
    _s(date(2026, 8, 6), "z2", "Easy Z2", 45),
    _s(date(2026, 8, 7), "gym_a", "Strength A — reduced (drop one set from everything)", 50),
    _s(date(2026, 8, 8), "long_ride", "75 min easy endurance", 75, priority="high"),
    # Week 5 — build
    _s(date(2026, 8, 11), "intervals", "3×15 min Sweet Spot", 75),
    _s(date(2026, 8, 12), "gym_a", "Strength A", 60),
    _s(date(2026, 8, 14), "tempo", "30 min continuous tempo", 70),
    _s(date(2026, 8, 15), "long_ride", "FIRST LOADED RIDE — 90 min (2.5–3 hr if time allows)", 90,
       loaded=True, priority="high",
       details="Bags on, ~15 lbs. Start practicing eating on the bike: one gel/bar per 45 min."),
    _s(date(2026, 8, 16), "z2", "Back-to-back Z2", 60),
    # Week 6 — build
    _s(date(2026, 8, 18), "intervals", "2×20 min Sweet Spot", 75, details="8 min easy between"),
    _s(date(2026, 8, 19), "z2", "Easy Z2 spin", 60),
    _s(date(2026, 8, 21), "gym_a", "Strength A — last full-strength session", 60),
    _s(date(2026, 8, 22), "long_ride", "90 min loaded, hilly, climbs at tempo", 90,
       loaded=True, priority="high"),
    _s(date(2026, 8, 23), "z2", "Back-to-back Z2, loaded", 75, loaded=True),
    # Week 7 — build, RECOVERY (exactly 4 non-rest sessions)
    _s(date(2026, 8, 25), "intervals", "2×12 min Sweet Spot (recovery week)", 60),
    _s(date(2026, 8, 27), "z2", "Easy Z2", 45),
    _s(date(2026, 8, 28), "gym_a", "Strength A — reduced sets", 50),
    _s(date(2026, 8, 29), "long_ride", "75 min easy", 75, priority="high"),
    # Week 8 — build
    _s(date(2026, 9, 1), "intervals", "2×20 min Sweet Spot", 75),
    _s(date(2026, 9, 2), "gym_b", "Maintenance B (strength maintenance starts)", 45),
    _s(date(2026, 9, 4), "tempo", "40 min continuous tempo", 75),
    _s(date(2026, 9, 5), "long_ride", "⭐ TRIP REHEARSAL — 3–4 hr fully loaded", 210,
       loaded=True, priority="critical",
       details="20–25 lbs, mostly Z2, gravel. Full kit, planned food and water stops."),
    _s(date(2026, 9, 6), "z2", "90 min Z2 loaded — day-two legs", 90, loaded=True),
    # Week 9 — specificity
    _s(date(2026, 9, 8), "intervals", "3×12 min Sweet Spot", 75),
    _s(date(2026, 9, 9), "z2", "Easy Z2 spin", 60),
    _s(date(2026, 9, 11), "gym_b", "Maintenance B", 45),
    _s(date(2026, 9, 12), "long_ride", "⭐ 3–4 hr loaded, gravel + hills", 210,
       loaded=True, priority="critical",
       details="Dial in exact trip nutrition: what, when, how much water."),
    _s(date(2026, 9, 13), "z2", "90–120 min Z2 loaded", 105, loaded=True,
       details="Note any contact-point pain (saddle/hands/feet) and fix NOW"),
    # Week 10 — specificity
    _s(date(2026, 9, 15), "intervals", "2×20 min Sweet Spot — LAST hard interval day", 75),
    _s(date(2026, 9, 16), "gym_b", "Maintenance B — FINAL gym session", 45),
    _s(date(2026, 9, 18), "z2", "Easy Z2", 60),
    _s(date(2026, 9, 19), "long_ride", "⭐ BIGGEST RIDE OF THE PLAN — 4 hr loaded, full trip setup", 240,
       loaded=True, priority="critical",
       details="Everything packed exactly as you'll ride the trip."),
    _s(date(2026, 9, 20), "z2", "2–2.5 hr Z2 loaded — camping substitute back-to-back", 135, loaded=True),
    # Week 11 — specificity
    _s(date(2026, 9, 22), "intervals", "3×8 min Sweet Spot (sharpness only)", 60),
    _s(date(2026, 9, 23), "z2", "Easy Z2", 45),
    _s(date(2026, 9, 26), "long_ride", "2–3 hr loaded easy — FINAL GEAR CHECK", 150,
       loaded=True, priority="high",
       details="After this ride, nothing new: no new saddle, shorts, shoes, or bags."),
    _s(date(2026, 9, 27), "z2", "Easy Z2", 75),
    # Week 12 — taper
    _s(date(2026, 9, 29), "z2", "Z2 + 3×3 min tempo", 60),
    _s(date(2026, 10, 1), "z2", "Easy spin + 2×2 min tempo", 45),
    _s(date(2026, 10, 3), "z2", "30 min easy spin (or rest)", 30),
    _s(date(2026, 10, 4), "rest", "TRIP START 🚴"),
]


def seed(session: Session, user_id: int, *, log: bool = True) -> dict[str, int]:
    """Upsert the whole plan for ``user_id``. Idempotent. Fills every unlisted day
    inside the plan window as a rest day. Returns {sessions, rest_days}."""
    listed = {row["date"] for row in PLAN}

    for row in PLAN:
        d = row["date"]
        week = tp.week_of(d)
        tp.upsert_session(
            session, user_id, d,
            week=week, phase=tp.phase_for_week(week),
            session_type=row["session_type"], title=row["title"],
            details=row["details"], duration_min=row["duration_min"],
            loaded=row["loaded"], priority=row["priority"], commit=False,
        )

    rest_days = 0
    d = tp.PLAN_START
    while d <= tp.PLAN_END:
        if d not in listed:
            week = tp.week_of(d)
            tp.upsert_session(
                session, user_id, d,
                week=week, phase=tp.phase_for_week(week),
                session_type="rest", title="Rest", commit=False,
            )
            rest_days += 1
        d += timedelta(days=1)

    counts = {"sessions": len(PLAN), "rest_days": rest_days}
    if log:
        tp.log_action(
            session, user_id, action="seeded", source="system",
            detail=counts, commit=False,
        )
    session.commit()
    logger.info("Seeded training plan for user %s: %s", user_id, counts)
    return counts


def _resolve_user_id(session: Session) -> int:
    """Pick the user to seed. Prefers ADMIN_TELEGRAM_ID; else the only user."""
    from app.config import settings
    from app.models.user import User
    from sqlalchemy import select

    admin_id = getattr(settings, "admin_telegram_id", None)
    if admin_id:
        u = session.scalar(select(User).where(User.telegram_id == int(admin_id)))
        if u:
            return u.id
    users = session.scalars(select(User).order_by(User.id)).all()
    if not users:
        raise SystemExit("No users found — run /start in Telegram first.")
    if len(users) > 1:
        raise SystemExit(
            f"Multiple users found ({[u.id for u in users]}); set ADMIN_TELEGRAM_ID to disambiguate."
        )
    return users[0].id


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    from app.db import SessionLocal

    with SessionLocal() as session:
        user_id = _resolve_user_id(session)
        counts = seed(session, user_id)
    print(f"Seeded plan for user {user_id}: {counts}")


if __name__ == "__main__":
    main()
