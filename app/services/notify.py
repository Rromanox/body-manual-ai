"""Bounded-retry Telegram send (Fix #6 reliability).

A scheduled job's ``bot.send_message`` can fail on a transient Telegram rate
limit / network blip and lose the message silently. ``send_with_retry`` retries
with exponential backoff and, if it still fails, logs and returns None (never
raises into the job). Used for the med reminder, where a lost message matters.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def send_with_retry(
    bot: Any,
    chat_id: int,
    text: str,
    *,
    reply_markup: Any | None = None,
    retries: int = 3,
    base_delay: float = 1.0,
) -> Any | None:
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            return await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
        except Exception as exc:  # noqa: BLE001 — retried/logged below
            last_exc = exc
            logger.warning(
                "Telegram send failed (attempt %d/%d) to %s: %s", attempt + 1, retries, chat_id, exc
            )
            if attempt < retries - 1:
                await asyncio.sleep(base_delay * (2 ** attempt))
    logger.error("Telegram send gave up after %d attempts to %s: %s", retries, chat_id, last_exc)
    return None
