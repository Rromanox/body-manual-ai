"""Unit 9: morning-block integration — rule-4 conversion, busy/tired, send_today_block."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.services import training_nl as nl
from app.services import training_plan as tp
from app.services import training_rules as rules
from scripts.seed_training_plan import seed
from tests.conftest import _TABLES, make_user


def test_detect_busy_or_tired():
    assert nl.detect_busy_or_tired("body tired") == "tired"
    assert nl.detect_busy_or_tired("wiped") == "tired"
    assert nl.detect_busy_or_tired("life's busy") == "busy"
    assert nl.detect_busy_or_tired("swamped at work") == "busy"
    assert nl.detect_busy_or_tired("what should I do?") is None
    assert nl.detect_busy_or_tired("x" * 50) is None


def test_apply_tired_conversion(mem_session):
    make_user(mem_session)
    seed(mem_session, 1)
    nq = rules.apply_tired_conversion(mem_session, 1, date(2026, 7, 15))
    assert nq.date == date(2026, 7, 21)  # next intervals after Jul 15
    assert tp.get_session(mem_session, 1, date(2026, 7, 21)).session_type == "z2"


def test_apply_tired_conversion_none_when_no_quality(mem_session):
    make_user(mem_session)
    seed(mem_session, 1)
    # After the last hard session there's nothing to convert.
    assert rules.apply_tired_conversion(mem_session, 1, date(2026, 10, 3)) is None


def _mk_sessionmaker():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=_TABLES)
    return sessionmaker(bind=engine, expire_on_commit=False)


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"ok": True}


def test_send_today_block_renders_and_stamps_presented(monkeypatch):
    from app.telegram import training_handlers as th

    Session = _mk_sessionmaker()
    monkeypatch.setattr(th, "SessionLocal", Session)
    monkeypatch.setattr(th, "log_outgoing", lambda *a, **k: None)

    with Session() as s:
        make_user(s, 1)
        seed(s, 1)

    bot = _FakeBot()
    now = datetime(2026, 9, 1, 7, 0, tzinfo=timezone.utc)  # week 8, 2×20 SS
    asyncio.run(th.send_today_block(bot, 1, 123, now, source="system"))

    assert len(bot.sent) == 1
    text = bot.sent[0]["text"]
    assert "🚴 TODAY — Week 8, Build" in text
    assert "2×20 min Sweet Spot (75 min)" in text
    assert "Recovery: no data yet → as written" in text     # missing WHOOP data path
    assert "Critical rides remaining: 3 of 3" in text

    with Session() as s:
        assert tp.get_session(s, 1, date(2026, 9, 1)).presented_at is not None


def test_send_today_block_noop_outside_plan(monkeypatch):
    from app.telegram import training_handlers as th

    Session = _mk_sessionmaker()
    monkeypatch.setattr(th, "SessionLocal", Session)
    monkeypatch.setattr(th, "log_outgoing", lambda *a, **k: None)
    with Session() as s:
        make_user(s, 1)
        seed(s, 1)

    bot = _FakeBot()
    now = datetime(2026, 11, 1, 7, 0, tzinfo=timezone.utc)  # after the plan ends
    asyncio.run(th.send_today_block(bot, 1, 123, now, source="system"))
    assert bot.sent == []
