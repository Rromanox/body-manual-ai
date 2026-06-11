"""Daily WHOOP pull: fetch, normalize to local waking dates, upsert daily_metrics.

Used three ways: the scheduled morning job, /today's pull-at-send-time, and the
30-day backfill right after OAuth connect.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.daily_metric import DailyMetric
from app.models.oauth_connection import OAuthConnection
from app.models.user import User
from app.services.alerts import send_admin_alert
from app.services.metrics_normalizer import (
    WHOOP_METRIC_FIELDS,
    DailyRow,
    normalize_whoop_data,
)
from app.services.whoop_client import WhoopAuthError, ensure_fresh_access_token, pull_raw

logger = logging.getLogger(__name__)


async def pull_and_store(session: Session, user: User, days: int = 7) -> int:
    """Pulls the last `days` of WHOOP data for one user and upserts daily_metrics.
    Returns the number of local dates written. Raises WhoopAuthError if the
    connection is missing, broken, or rejected."""
    connection = session.scalar(
        select(OAuthConnection).where(
            OAuthConnection.user_id == user.id, OAuthConnection.provider == "whoop"
        )
    )
    if connection is None or connection.status != "active":
        raise WhoopAuthError(f"No active WHOOP connection for user {user.id}")

    access_token = await ensure_fresh_access_token(session, connection)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    raw = await pull_raw(access_token, start, end)
    rows = normalize_whoop_data(
        raw.cycles, raw.sleeps, raw.recoveries, raw.workouts, ZoneInfo(user.timezone)
    )
    _upsert_daily_rows(session, user.id, rows)
    return len(rows)


def _upsert_daily_rows(session: Session, user_id: int, rows: dict) -> None:
    for day, draft in rows.items():
        metric = session.scalar(
            select(DailyMetric).where(DailyMetric.user_id == user_id, DailyMetric.date == day)
        )
        if metric is None:
            metric = DailyMetric(user_id=user_id, date=day)
            session.add(metric)
        _apply_draft(metric, draft)
    session.commit()


def _apply_draft(metric: DailyMetric, draft: DailyRow) -> None:
    # None means "no data in this pull", never "erase" — partial windows must not
    # null out values written by an earlier, wider pull
    for field_name in WHOOP_METRIC_FIELDS:
        value = getattr(draft, field_name)
        if value is not None:
            setattr(metric, field_name, value)
    metric.raw_whoop_json = draft.raw


async def run_daily_pull() -> None:
    """Scheduled morning job: refresh every active user's recent WHOOP data."""
    with SessionLocal() as session:
        users = session.scalars(
            select(User)
            .join(OAuthConnection, OAuthConnection.user_id == User.id)
            .where(OAuthConnection.provider == "whoop", OAuthConnection.status == "active")
        ).all()
        for user in users:
            try:
                written = await pull_and_store(session, user, days=7)
                logger.info("Daily pull for user %s wrote %s dates", user.id, written)
            except Exception as exc:
                logger.exception("Daily pull failed for user %s", user.id)
                await send_admin_alert(f"Daily pull failed for user {user.id}: {exc}")
