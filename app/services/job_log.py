"""Job-execution audit log (Fix #2).

Wrap each scheduled job with ``logged_job`` so every run records name, start/finish,
success/error, and an optional detail dict. This is what makes "when did the
reminder actually fire?" answerable without grepping raw logs.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.job_run import JobRun

logger = logging.getLogger(__name__)


def record_run(
    session: Session,
    job_name: str,
    *,
    status: str,
    detail: dict[str, Any] | None,
    started_at: datetime,
    finished_at: datetime,
) -> None:
    session.add(JobRun(
        job_name=job_name, status=status, detail=detail or {},
        started_at=started_at, finished_at=finished_at,
    ))
    session.commit()


def _default_recorder(job_name, status, detail, started_at, finished_at) -> None:
    with SessionLocal() as session:
        record_run(session, job_name, status=status, detail=detail,
                   started_at=started_at, finished_at=finished_at)


async def logged_job(
    job_name: str,
    fn: Callable[[], Awaitable[Any]],
    *,
    recorder: Callable[..., None] | None = None,
) -> None:
    """Run ``fn`` and record the outcome. Never raises — a logging wrapper must
    not take down the scheduler. If ``fn`` returns a dict, it's stored as detail."""
    started = datetime.now(timezone.utc)
    status = "success"
    detail: dict[str, Any] = {}
    try:
        result = await fn()
        if isinstance(result, dict):
            detail = result
    except Exception as exc:  # noqa: BLE001 — logged + recorded, never re-raised
        status = "error"
        detail = {"error": f"{type(exc).__name__}: {exc}"}
        logger.exception("Scheduled job %s failed", job_name)
    finished = datetime.now(timezone.utc)
    try:
        (recorder or _default_recorder)(job_name, status, detail, started, finished)
    except Exception:
        logger.exception("Failed to record job run for %s", job_name)
