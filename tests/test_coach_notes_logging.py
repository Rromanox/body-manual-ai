"""Fix #5: _update_coach_notes must log failures instead of swallowing them."""
from __future__ import annotations

import asyncio


class _Boom:
    def __enter__(self):
        raise RuntimeError("db down")

    def __exit__(self, *a):
        return False


def test_update_coach_notes_logs_and_does_not_raise(monkeypatch, caplog):
    from app.telegram import handlers

    # Make the first DB access blow up.
    monkeypatch.setattr(handlers, "SessionLocal", lambda: _Boom())

    with caplog.at_level("ERROR"):
        # must not raise
        asyncio.run(handlers._update_coach_notes(1, "question", "answer"))

    assert any("coach_notes update failed" in r.getMessage() for r in caplog.records)
