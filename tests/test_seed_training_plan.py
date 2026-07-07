"""Unit 3: seed script — idempotency + correctness spot-checks + user resolution."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.models.training_session import TrainingSession
from app.services import training_plan as tp
from scripts import seed_training_plan as seed_mod
from scripts.seed_training_plan import seed
from tests.conftest import make_user


def _count(session, user_id=1) -> int:
    return session.query(TrainingSession).filter_by(user_id=user_id).count()


def test_seed_is_idempotent(mem_session):
    make_user(mem_session)
    seed(mem_session, 1)
    first = _count(mem_session)
    seed(mem_session, 1)
    second = _count(mem_session)
    assert first == second
    # Full plan window Jul 13 – Oct 4 2026 inclusive = 84 calendar days.
    assert first == 84


def test_seed_spot_checks(mem_session):
    make_user(mem_session)
    seed(mem_session, 1)

    biggest = tp.get_session(mem_session, 1, date(2026, 9, 19))
    assert biggest.priority == "critical"
    assert biggest.loaded is True
    assert biggest.duration_min == 240
    assert biggest.session_type == "long_ride"

    aug3 = tp.get_session(mem_session, 1, date(2026, 8, 3))
    assert aug3.session_type == "rest"

    week7 = tp.get_week(mem_session, 1, 7)
    non_rest = [s for s in week7 if s.session_type != "rest"]
    assert len(non_rest) == 4


def test_seed_derives_week_and_phase(mem_session):
    make_user(mem_session)
    seed(mem_session, 1)
    # Aug 25 is week 7, which is a build phase (recovery week is still "build").
    aug25 = tp.get_session(mem_session, 1, date(2026, 8, 25))
    assert aug25.week == 7
    assert aug25.phase == "build"
    # Sep 29 week 12 taper.
    sep29 = tp.get_session(mem_session, 1, date(2026, 9, 29))
    assert sep29.week == 12
    assert sep29.phase == "taper"
    # Trip start marker.
    trip = tp.get_session(mem_session, 1, date(2026, 10, 4))
    assert trip.session_type == "rest"
    assert "TRIP START" in trip.title


def test_exactly_three_critical_rides(mem_session):
    make_user(mem_session)
    seed(mem_session, 1)
    crit = tp.critical_rides(mem_session, 1)
    assert [s.date for s in crit] == [date(2026, 9, 5), date(2026, 9, 12), date(2026, 9, 19)]


# --- user resolution: raise loudly, never seed silently ---------------------

def _patch_admin(monkeypatch, value):
    # settings is a frozen dataclass; swap the whole singleton (re-imported inside
    # _resolve_user_id at call time) for one carrying just the field we need.
    monkeypatch.setattr("app.config.settings", SimpleNamespace(admin_telegram_id=value))


def test_resolve_raises_when_admin_unset(mem_session, monkeypatch):
    _patch_admin(monkeypatch, 0)  # unset
    make_user(mem_session, 1)  # a user exists, but no admin configured
    with pytest.raises(SystemExit):
        seed_mod._resolve_user_id(mem_session)


def test_resolve_raises_when_admin_matches_no_user(mem_session, monkeypatch):
    _patch_admin(monkeypatch, 999999)
    make_user(mem_session, 1)  # telegram_id 1001, not 999999
    with pytest.raises(SystemExit):
        seed_mod._resolve_user_id(mem_session)


def test_resolve_returns_admin_user(mem_session, monkeypatch):
    make_user(mem_session, 1)  # make_user sets telegram_id = 1000 + id = 1001
    _patch_admin(monkeypatch, 1001)
    assert seed_mod._resolve_user_id(mem_session) == 1


def test_seed_if_empty_only_runs_when_empty(mem_session):
    make_user(mem_session)
    first = seed_mod.seed_if_empty(mem_session, 1)
    assert first is not None and first["sessions"] == len(seed_mod.PLAN)
    assert _count(mem_session) == 84
    # An edit that a re-boot must not clobber.
    tp.edit_session(mem_session, 1, date(2026, 7, 14), duration_min=999, source="command")
    # Second call is a no-op because a plan already exists.
    assert seed_mod.seed_if_empty(mem_session, 1) is None
    assert _count(mem_session) == 84
    assert tp.get_session(mem_session, 1, date(2026, 7, 14)).duration_min == 999  # edit preserved
