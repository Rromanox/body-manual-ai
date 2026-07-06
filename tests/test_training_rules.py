"""Unit 4: skip / reschedule rules (all six)."""
from __future__ import annotations

from datetime import date

from app.models.training_log import TrainingLog
from app.services import training_plan as tp
from app.services import training_rules as tr
from scripts.seed_training_plan import seed
from tests.conftest import make_user


def _seeded(session):
    make_user(session)
    seed(session, 1)


def _last_log_rule(session, action):
    row = (
        session.query(TrainingLog)
        .filter_by(user_id=1, action=action)
        .order_by(TrainingLog.id.desc())
        .first()
    )
    return row.detail.get("rule") if row else None


# Rule 1 — normal session: skip, no reschedule.
def test_rule1_normal_skip_no_reschedule(mem_session):
    _seeded(mem_session)
    out = tr.skip_session(mem_session, 1, date(2026, 7, 14), reason="busy")  # Tue intervals
    assert out["outcome"] == "skipped"
    assert out["rule"] == "normal_no_reschedule"
    assert tp.get_session(mem_session, 1, date(2026, 7, 14)).status == "skipped"
    assert _last_log_rule(mem_session, "skipped") == "normal_no_reschedule"


# Rule 2 — high Saturday ride shifts to Sunday, canceling Sunday's Z2.
def test_rule2_high_saturday_to_sunday(mem_session):
    _seeded(mem_session)
    out = tr.skip_session(mem_session, 1, date(2026, 7, 18))  # Sat, high
    assert out["outcome"] == "moved"
    assert out["rule"] == "high_saturday_to_sunday"
    sat = tp.get_session(mem_session, 1, date(2026, 7, 18))
    sun = tp.get_session(mem_session, 1, date(2026, 7, 19))
    assert sat.status == "moved"
    assert sun.session_type == "long_ride"
    assert sun.title == "90 min endurance"
    assert sun.moved_from == date(2026, 7, 18)
    assert sun.status == "pending"
    assert out["canceled_target"]["type"] == "z2"


# Rule 2 fallback — Sunday in a protected (recovery) week can't gain the session.
def test_rule2_protected_sunday_skips(mem_session):
    _seeded(mem_session)
    out = tr.skip_session(mem_session, 1, date(2026, 8, 8))  # Sat, high, week 4 (Sun Aug 9 protected)
    assert out["outcome"] == "skipped"
    assert out["rule"] == "high_saturday_sunday_taken"
    assert tp.get_session(mem_session, 1, date(2026, 8, 8)).status == "skipped"
    # Aug 9 stays a rest day — no session added to the recovery week.
    assert tp.get_session(mem_session, 1, date(2026, 8, 9)).session_type == "rest"


# Rule 2 fallback — Sunday occupied by an immovable (non-rest/non-z2) session.
def test_rule2_immovable_sunday_skips(mem_session):
    _seeded(mem_session)
    # Force Jul 19 (Sun, week 1) to a gym session so it can't be overwritten.
    tp.upsert_session(
        mem_session, 1, date(2026, 7, 19), week=1, phase="base",
        session_type="gym_a", title="Extra gym",
    )
    out = tr.skip_session(mem_session, 1, date(2026, 7, 18))
    assert out["outcome"] == "skipped"
    assert out["rule"] == "high_saturday_sunday_taken"


# Rule 3 — critical ride: never dropped silently; produces the two-button choice.
def test_rule3_critical_needs_choice(mem_session):
    _seeded(mem_session)
    out = tr.skip_session(mem_session, 1, date(2026, 9, 5))  # critical
    assert out["outcome"] == "needs_choice"
    assert out["rule"] == "critical_no_silent_drop"
    opts = {o["choice"]: o["to"] for o in out["options"]}
    assert opts["sunday"] == date(2026, 9, 6)
    assert opts["next_saturday"] == date(2026, 9, 12)
    # Not silently skipped: still pending, but logged unresolved.
    assert tp.get_session(mem_session, 1, date(2026, 9, 5)).status == "pending"
    assert _last_log_rule(mem_session, "skipped") == "critical_no_silent_drop"


def test_rule3_apply_choice_sunday(mem_session):
    _seeded(mem_session)
    tr.skip_session(mem_session, 1, date(2026, 9, 5))
    out = tr.apply_critical_choice(mem_session, 1, date(2026, 9, 5), "sunday")
    assert out["outcome"] == "moved"
    assert tp.get_session(mem_session, 1, date(2026, 9, 5)).status == "moved"
    sun = tp.get_session(mem_session, 1, date(2026, 9, 6))
    assert sun.priority == "critical"
    assert sun.moved_from == date(2026, 9, 5)


# Rule 5 — move INTO a protected week is rejected.
def test_rule5_move_into_week7_rejected(mem_session):
    _seeded(mem_session)
    out = tr.move_session(mem_session, 1, date(2026, 7, 14), date(2026, 8, 26))  # Aug 26 = week 7
    assert out["outcome"] == "rejected"
    assert out["rule"] == "protected_week"
    assert out["week"] == 7
    # Source unchanged.
    assert tp.get_session(mem_session, 1, date(2026, 7, 14)).status == "pending"


# /move to a rest day works; to a non-rest day asks to confirm a swap.
def test_move_to_rest_and_swap(mem_session):
    _seeded(mem_session)
    # Jul 16 (Thu) is a rest day in week 1.
    out = tr.move_session(mem_session, 1, date(2026, 7, 14), date(2026, 7, 16))
    assert out["outcome"] == "moved"
    assert tp.get_session(mem_session, 1, date(2026, 7, 16)).session_type == "intervals"
    assert tp.get_session(mem_session, 1, date(2026, 7, 14)).status == "moved"

    # Moving onto an occupied day needs confirmation, then swaps.
    out2 = tr.move_session(mem_session, 1, date(2026, 7, 15), date(2026, 7, 17))
    assert out2["outcome"] == "needs_confirm_swap"
    out3 = tr.move_session(mem_session, 1, date(2026, 7, 15), date(2026, 7, 17), confirm_swap=True)
    assert out3["outcome"] == "swapped"


# Rule 4 — two consecutive skips detected; next quality session identified.
def test_rule4_consecutive_skips(mem_session):
    _seeded(mem_session)
    tr.skip_session(mem_session, 1, date(2026, 7, 14))  # intervals
    tr.skip_session(mem_session, 1, date(2026, 7, 15))  # z2
    assert tr.consecutive_skips(mem_session, 1, date(2026, 7, 16)) == 2
    nxt = tr.next_quality_session(mem_session, 1, date(2026, 7, 16))
    assert nxt.date == date(2026, 7, 21)  # next intervals
    assert nxt.session_type == "intervals"


# Rule 6 — mutations write training_log naming the rule.
def test_rule6_every_mutation_logs_rule(mem_session):
    _seeded(mem_session)
    tr.move_session(mem_session, 1, date(2026, 7, 14), date(2026, 7, 16))
    moved = (
        mem_session.query(TrainingLog).filter_by(user_id=1, action="moved").first()
    )
    assert moved is not None
    assert moved.detail.get("rule") == "move_to_rest_day"
