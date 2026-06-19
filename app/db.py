from __future__ import annotations

from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

# JSONB on Postgres (prod), JSON on SQLite (tests). Same dict/list round-trip
# either way, so prod behavior is unchanged while the test suite can run on an
# in-memory SQLite database with no Postgres dependency. New models should use
# this instead of importing postgresql.JSONB directly.
JSONVariant = JSONB().with_variant(JSON(), "sqlite")


class Base(DeclarativeBase):
    pass


engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
