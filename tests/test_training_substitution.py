"""Unit 6: substitution engine — every table cell, critical refusal, 3/week flag."""
from __future__ import annotations

from datetime import date

from app.services import training_plan as tp
from app.services import training_substitution as sub
from scripts.seed_training_plan import seed
from tests.conftest import make_user


def _seeded(session):
    make_user(session)
    seed(session, 1)


# --- LESS TIME (keep intensity, cut volume) ---------------------------------

def test_less_time_needs_minutes_then_applies(mem_session):
    _seeded(mem_session)
    out = sub.substitute(mem_session, 1, date(2026, 7, 14), "less_time")  # intervals
    assert out["outcome"] == "needs_minutes"
    assert out["options"] == [30, 45, 60]
    out2 = sub.substitute(mem_session, 1, date(2026, 7, 14), "less_time", minutes=30)
    assert out2["outcome"] == "substituted"
    assert "2×8" in out2["text"]
    assert tp.get_session(mem_session, 1, date(2026, 7, 14)).status == "modified"


def test_less_time_cells(mem_session):
    _seeded(mem_session)
    assert "2×15" in sub.substitute(mem_session, 1, date(2026, 7, 14), "less_time", minutes=60)["text"]
    # tempo (Aug 14), z2 (Jul 15), long_ride (Jul 18), gym (Jul 17)
    assert "tempo block" in sub.substitute(mem_session, 1, date(2026, 8, 14), "less_time", minutes=45)["text"]
    assert "Z2" in sub.substitute(mem_session, 1, date(2026, 7, 15), "less_time", minutes=30)["text"]
    long_out = sub.substitute(mem_session, 1, date(2026, 7, 18), "less_time", minutes=60)
    assert "endurance pace" in long_out["text"]
    assert "core" in sub.substitute(mem_session, 1, date(2026, 7, 17), "less_time", minutes=45)["text"]


def test_less_time_shortened_long_ride_flagged(mem_session):
    _seeded(mem_session)
    sub.substitute(mem_session, 1, date(2026, 8, 15), "less_time", minutes=60)  # loaded long ride
    from app.models.training_log import TrainingLog
    row = mem_session.query(TrainingLog).filter_by(action="substituted").first()
    assert row.detail.get("shortened_long_ride") is True
    assert ", loaded" in row.detail["substitution"]


# --- NO BIKE ----------------------------------------------------------------

def test_no_bike_cells(mem_session):
    _seeded(mem_session)
    assert "stair climbs" in sub.substitute(mem_session, 1, date(2026, 7, 14), "no_bike")["text"]  # intervals
    assert "stair climbs" in sub.substitute(mem_session, 1, date(2026, 8, 14), "no_bike")["text"]  # tempo
    assert "walk" in sub.substitute(mem_session, 1, date(2026, 7, 15), "no_bike")["text"]          # z2
    # long ride not substitutable off-bike -> suggests a move.
    long_out = sub.substitute(mem_session, 1, date(2026, 7, 18), "no_bike")
    assert long_out["outcome"] == "not_substitutable"
    assert long_out["suggest"] == "move"
    # gym needs no bike -> noop with a friendly message.
    gym_out = sub.substitute(mem_session, 1, date(2026, 7, 17), "no_bike")
    assert gym_out["outcome"] == "noop"


# --- CAN'T LEAVE / TRAVELING ------------------------------------------------

def test_cant_leave_is_maintenance_circuit(mem_session):
    _seeded(mem_session)
    out = sub.substitute(mem_session, 1, date(2026, 7, 18), "cant_leave")  # long ride
    assert out["outcome"] == "substituted"
    assert out["maintenance_only"] is True
    assert "bodyweight circuit" in out["text"]
    # gym too
    gym = sub.substitute(mem_session, 1, date(2026, 7, 17), "cant_leave")
    assert gym["maintenance_only"] is True


# --- routing ----------------------------------------------------------------

def test_feeling_beat_routes_to_gate_and_skip_routes_to_skip(mem_session):
    _seeded(mem_session)
    assert sub.substitute(mem_session, 1, date(2026, 7, 14), "feeling_beat")["outcome"] == "route_to_gate"
    assert sub.substitute(mem_session, 1, date(2026, 7, 14), "skip")["outcome"] == "route_to_skip"
    # Neither mutates the session.
    assert tp.get_session(mem_session, 1, date(2026, 7, 14)).status == "pending"


# --- hard constraint 1: critical rides never substituted --------------------

def test_critical_ride_refused(mem_session):
    _seeded(mem_session)
    for c in ("less_time", "no_bike", "cant_leave"):
        out = sub.substitute(mem_session, 1, date(2026, 9, 19), c, minutes=30)
        assert out["outcome"] == "refused_critical"
    assert tp.get_session(mem_session, 1, date(2026, 9, 19)).status == "pending"


# --- hard constraint 3: 3+ substitutions in one week flags ------------------

def test_three_per_week_flag(mem_session):
    _seeded(mem_session)
    sub.substitute(mem_session, 1, date(2026, 7, 14), "cant_leave")
    sub.substitute(mem_session, 1, date(2026, 7, 15), "cant_leave")
    assert sub.substitutions_this_week(mem_session, 1, 1) == 2
    assert sub.too_many_substitutions(mem_session, 1, 1) is False
    sub.substitute(mem_session, 1, date(2026, 7, 17), "cant_leave")
    assert sub.substitutions_this_week(mem_session, 1, 1) == 3
    assert sub.too_many_substitutions(mem_session, 1, 1) is True
