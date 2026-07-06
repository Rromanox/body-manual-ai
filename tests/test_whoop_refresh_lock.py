"""Fix #4: WHOOP token refresh must be serialized per user (like Withings) so a
rotating refresh token isn't invalidated by a concurrent double-refresh."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services import whoop_client


class _FakeSession:
    def refresh(self, obj):  # no-op; the object is already the live instance
        pass

    def commit(self):
        pass


def test_concurrent_refresh_calls_token_endpoint_once(monkeypatch):
    calls: list[int] = []

    async def fake_token_request(data):
        calls.append(1)
        await asyncio.sleep(0.02)  # hold the lock long enough for the second call to queue
        return {"access_token": "new-tok", "refresh_token": "r2", "expires_in": 3600}

    monkeypatch.setattr(whoop_client, "_token_request", fake_token_request)
    whoop_client._token_refresh_locks.clear()

    past = datetime.now(timezone.utc) - timedelta(hours=1)
    conn = SimpleNamespace(user_id=99, access_token="old", refresh_token="r1", expires_at=past, status="active")
    session = _FakeSession()

    async def run():
        await asyncio.gather(
            whoop_client.ensure_fresh_access_token(session, conn),
            whoop_client.ensure_fresh_access_token(session, conn),
        )

    asyncio.run(run())
    assert len(calls) == 1          # lock + re-check dedups the refresh
    assert conn.access_token == "new-tok"


def test_not_refreshed_when_still_valid(monkeypatch):
    async def fail_if_called(data):
        raise AssertionError("should not refresh a still-valid token")

    monkeypatch.setattr(whoop_client, "_token_request", fail_if_called)
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    conn = SimpleNamespace(user_id=1, access_token="good", refresh_token="r", expires_at=future, status="active")
    assert asyncio.run(whoop_client.ensure_fresh_access_token(_FakeSession(), conn)) == "good"
