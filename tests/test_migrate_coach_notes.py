"""Tests for the manual coach_notes -> user_memories migration (Phase 1).

Covers the pure mapping (convert_coach_notes) and the DB behavior: idempotent,
non-destructive, dry-run writes nothing.
"""
from __future__ import annotations

from app.models.user_memory import UserMemory
from app.services import migrate_coach_notes as mig
from tests.conftest import make_user


# --- pure mapping -----------------------------------------------------------

def test_convert_empty_returns_empty():
    assert mig.convert_coach_notes({}) == []
    assert mig.convert_coach_notes(None) == []
    assert mig.convert_coach_notes("garbage") == []


def test_convert_maps_known_keys():
    notes = {
        "supplements": ["creatine 5g/day"],
        "medications": ["albuterol"],
        "health_context": ["asthmatic"],
        "goals": ["lose weight, target under 190 lbs"],
    }
    out = {c["content"]: c for c in mig.convert_coach_notes(notes)}
    assert out["creatine 5g/day"]["memory_type"] == "stable_fact"
    assert out["creatine 5g/day"]["tags"] == ["supplement"]
    assert out["albuterol"]["tags"] == ["medication"]
    assert out["asthmatic"]["tags"] == ["health"]
    assert out["lose weight, target under 190 lbs"]["memory_type"] == "goal"


def test_convert_routes_lifestyle_by_content():
    notes = {"lifestyle": ["trains at the gym 4x/week", "works night shifts", "vegetarian"]}
    out = {c["content"]: c["memory_type"] for c in mig.convert_coach_notes(notes)}
    assert out["trains at the gym 4x/week"] == "training_preference"
    assert out["works night shifts"] == "schedule_pattern"
    assert out["vegetarian"] == "stable_fact"


def test_convert_routes_other_temporary_vs_stable():
    notes = {"other": ["traveling for work until March", "left-handed"]}
    out = {c["content"]: c["memory_type"] for c in mig.convert_coach_notes(notes)}
    assert out["traveling for work until March"] == "context_event"
    assert out["left-handed"] == "stable_fact"


def test_convert_handles_scalar_values():
    out = mig.convert_coach_notes({"goals": "run a 5k"})
    assert len(out) == 1
    assert out[0]["content"] == "run a 5k"
    assert out[0]["memory_type"] == "goal"


def test_convert_import_provenance():
    out = mig.convert_coach_notes({"supplements": ["creatine"]})
    assert out[0]["source"] == "ai_extracted"
    assert out[0]["confidence"] == "medium"


# --- DB behavior ------------------------------------------------------------

def test_migrate_user_writes_rows(mem_session):
    user = make_user(mem_session, coach_notes={
        "supplements": ["creatine 5g/day"],
        "goals": ["lose weight"],
    })
    written = mig.migrate_user(mem_session, user, dry_run=False)
    assert len(written) == 2
    rows = mem_session.query(UserMemory).all()
    assert {r.content for r in rows} == {"creatine 5g/day", "lose weight"}
    # coach_notes left completely intact (non-destructive)
    assert user.coach_notes == {"supplements": ["creatine 5g/day"], "goals": ["lose weight"]}


def test_migrate_user_dry_run_writes_nothing(mem_session):
    user = make_user(mem_session, coach_notes={"supplements": ["creatine"]})
    candidates = mig.migrate_user(mem_session, user, dry_run=True)
    assert len(candidates) == 1
    assert mem_session.query(UserMemory).count() == 0


def test_migrate_user_is_idempotent(mem_session):
    user = make_user(mem_session, coach_notes={"supplements": ["creatine"], "goals": ["cut to 185"]})
    mig.migrate_user(mem_session, user, dry_run=False)
    mig.migrate_user(mem_session, user, dry_run=False)  # run again
    rows = mem_session.query(UserMemory).all()
    assert len(rows) == 2  # no duplicates
    # second run reinforced evidence rather than inserting
    assert all(r.evidence_count == 2 for r in rows)


def test_migrate_all_skips_empty_coach_notes(mem_session):
    make_user(mem_session, user_id=1, coach_notes={})
    make_user(mem_session, user_id=2, coach_notes={"goals": ["x"]})
    summary = mig.migrate_all(mem_session, dry_run=False)
    assert set(summary.keys()) == {2}
    assert mem_session.query(UserMemory).count() == 1


def test_migrate_all_can_target_one_user(mem_session):
    make_user(mem_session, user_id=1, coach_notes={"goals": ["a"]})
    make_user(mem_session, user_id=2, coach_notes={"goals": ["b"]})
    summary = mig.migrate_all(mem_session, dry_run=False, user_id=2)
    assert set(summary.keys()) == {2}
    rows = mem_session.query(UserMemory).all()
    assert {r.content for r in rows} == {"b"}
