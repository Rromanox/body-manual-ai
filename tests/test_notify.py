"""Fix #6: bounded-retry Telegram send."""
from __future__ import annotations

import asyncio

from app.services import notify


def test_send_with_retry_succeeds_after_transient_failures():
    calls: list[int] = []

    class Bot:
        async def send_message(self, **kwargs):
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("rate limited")
            return "ok"

    out = asyncio.run(notify.send_with_retry(Bot(), 1, "hi", base_delay=0))
    assert out == "ok" and len(calls) == 3


def test_send_with_retry_gives_up_without_raising():
    class Bot:
        async def send_message(self, **kwargs):
            raise RuntimeError("down")

    out = asyncio.run(notify.send_with_retry(Bot(), 1, "hi", retries=2, base_delay=0))
    assert out is None
