"""Shared test fixtures for DB-backed tests.

Uses an in-memory SQLite database with a StaticPool so the schema persists across
the session's connections. Only the tables the memory tests need are created, and
they rely on the JSONVariant shim (app.db) so JSONB columns compile to JSON on
SQLite. No Postgres required (Memory 2.0 plan §16).
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models.health_reminder import HealthReminder
from app.models.job_run import JobRun
from app.models.recommendation import RecommendationLedger
from app.models.training_log import TrainingLog
from app.models.training_session import TrainingSession
from app.models.user import User
from app.models.user_memory import UserMemory

_TABLES = [
    User.__table__,
    UserMemory.__table__,
    RecommendationLedger.__table__,
    HealthReminder.__table__,
    JobRun.__table__,
    TrainingSession.__table__,
    TrainingLog.__table__,
]


@pytest.fixture
def mem_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=_TABLES)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    session = TestSession()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def make_user(session: Session, user_id: int = 1, coach_notes: dict | None = None) -> User:
    user = User(
        id=user_id,
        telegram_id=1000 + user_id,
        first_name="Test",
        timezone="America/Detroit",
        coach_notes=coach_notes or {},
    )
    session.add(user)
    session.commit()
    return user
