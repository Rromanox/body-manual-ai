"""Unit 7 (addendum): callback handler tests — the tr_* taps actually persist.

These invoke training_handlers.training_callback end-to-end (with a fake Telegram
callback query and a monkeypatched SessionLocal) rather than only the underlying
service ops, proving the button taps write the right status + training_log rows.
"""
from __future__ import annotations

import asyncio
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models.training_log import TrainingLog
from app.services import training_plan as tp
from scripts.seed_training_plan import seed
from tests.conftest import _TABLES, make_user

TG_ID = 1001  # make_user sets telegram_id = 1000 + user_id


def _mk_sessionmaker():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=_TABLES)
    return sessionmaker(bind=engine, expire_on_commit=False)


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeQuery:
    def __init__(self, data, uid):
        self.data = data
        self.from_user = _FakeUser(uid)
        self.edited = None

    async def answer(self):
        pass

    async def edit_message_text(self, text, reply_markup=None):
        self.edited = {"text": text, "reply_markup": reply_markup}


class _FakeUpdate:
    def __init__(self, query):
        self.callback_query = query


class _FakeContext:
    def __init__(self):
        self.user_data = {}


def _fire(th, data):
    q = _FakeQuery(data, TG_ID)
    asyncio.run(th.training_callback(_FakeUpdate(q), _FakeContext()))
    return q


def _setup(monkeypatch):
    from app.telegram import training_handlers as th
    Session = _mk_sessionmaker()
    monkeypatch.setattr(th, "SessionLocal", Session)
    monkeypatch.setattr(th, "log_outgoing", lambda *a, **k: None)
    with Session() as s:
        make_user(s, 1)
        seed(s, 1)
    return th, Session


def test_critical_callback_persists_move_and_log(monkeypatch):
    th, Session = _setup(monkeypatch)
    q = _fire(th, "tr_crit:sunday:2026-09-05")  # Sep 5 is a critical ride
    assert "Moved" in q.edited["text"]
    with Session() as s:
        assert tp.get_session(s, 1, date(2026, 9, 5)).status == "moved"
        sun = tp.get_session(s, 1, date(2026, 9, 6))
        assert sun.priority == "critical"
        assert sun.moved_from == date(2026, 9, 5)
        moved = s.query(TrainingLog).filter_by(user_id=1, action="moved").all()
        assert len(moved) == 1
        assert moved[0].detail["rule"] == "critical_choice_sunday"


def test_gate_accept_callback_sets_modified_and_logs(monkeypatch):
    th, Session = _setup(monkeypatch)
    # An adjustment is on file (as the gate would have recorded it).
    with Session() as s:
        row = tp.get_session(s, 1, date(2026, 9, 1))  # week 8 intervals
        row.recovery_adjustment = "Reduce to tempo"
        s.commit()
    q = _fire(th, "tr_gate:accept:2026-09-01")
    assert "accepted" in q.edited["text"].lower()
    with Session() as s:
        assert tp.get_session(s, 1, date(2026, 9, 1)).status == "modified"
        logs = s.query(TrainingLog).filter_by(user_id=1, action="gate_accepted").all()
        assert len(logs) == 1
        assert logs[0].session_date == date(2026, 9, 1)


def test_gate_override_callback_logs_overridden_only(monkeypatch):
    th, Session = _setup(monkeypatch)
    q = _fire(th, "tr_gate:ride:2026-09-01")
    assert "as written" in q.edited["text"].lower()
    with Session() as s:
        # Overriding does not change the session status.
        assert tp.get_session(s, 1, date(2026, 9, 1)).status == "pending"
        assert s.query(TrainingLog).filter_by(user_id=1, action="gate_overridden").count() == 1
        assert s.query(TrainingLog).filter_by(user_id=1, action="gate_accepted").count() == 0


def test_move_swap_callback_swaps_two_days(monkeypatch):
    th, Session = _setup(monkeypatch)
    # Jul 15 (z2) onto Jul 17 (gym_a) — occupied day, confirmed swap.
    q = _fire(th, "tr_move:swap:2026-07-15:2026-07-17")
    assert "Swapped" in q.edited["text"]
    with Session() as s:
        assert tp.get_session(s, 1, date(2026, 7, 15)).session_type == "gym_a"
        assert tp.get_session(s, 1, date(2026, 7, 17)).session_type == "z2"
        assert s.query(TrainingLog).filter_by(user_id=1, action="moved").count() == 1
