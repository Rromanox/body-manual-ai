"""Fix #2: job-execution audit log."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.models.job_run import JobRun
from app.services import job_log


def test_record_run_writes_row(mem_session):
    now = datetime.now(timezone.utc)
    job_log.record_run(mem_session, "test_job", status="success", detail={"sent": 3},
                       started_at=now, finished_at=now)
    rows = mem_session.query(JobRun).all()
    assert len(rows) == 1
    assert rows[0].job_name == "test_job" and rows[0].status == "success"
    assert rows[0].detail == {"sent": 3}


def test_logged_job_records_success():
    captured: dict = {}

    def rec(job_name, status, detail, started_at, finished_at):
        captured.update(job_name=job_name, status=status, detail=detail)

    async def ok():
        return {"sent": 2}

    asyncio.run(job_log.logged_job("morning", ok, recorder=rec))
    assert captured["job_name"] == "morning"
    assert captured["status"] == "success"
    assert captured["detail"] == {"sent": 2}


def test_logged_job_records_error_and_does_not_raise():
    captured: dict = {}

    def rec(job_name, status, detail, started_at, finished_at):
        captured.update(status=status, detail=detail)

    async def boom():
        raise RuntimeError("kaboom")

    # must not propagate — a logging wrapper shouldn't kill the scheduler
    asyncio.run(job_log.logged_job("morning", boom, recorder=rec))
    assert captured["status"] == "error"
    assert "RuntimeError" in captured["detail"]["error"]
