"""Helpers for logging every bot message to message_log.

call log_incoming() at the top of every handler,
call log_outgoing() after every bot.send_message / reply_text.
"""
from __future__ import annotations

import logging

from app.db import SessionLocal
from app.models.message_log import MessageLog

logger = logging.getLogger(__name__)


def log_incoming(
    telegram_id: int,
    content: str,
    message_type: str,
    user_id: int | None = None,
) -> None:
    """Log a message arriving from the user (fire-and-forget, never raises)."""
    try:
        with SessionLocal() as session:
            session.add(MessageLog(
                user_id=user_id,
                telegram_id=telegram_id,
                direction="in",
                message_type=message_type,
                content=content,
            ))
            session.commit()
    except Exception:
        logger.exception("chat_logger.log_incoming failed — continuing")


def log_outgoing(
    telegram_id: int,
    content: str,
    message_type: str,
    user_id: int | None = None,
) -> None:
    """Log a message sent by the bot (fire-and-forget, never raises)."""
    try:
        with SessionLocal() as session:
            session.add(MessageLog(
                user_id=user_id,
                telegram_id=telegram_id,
                direction="out",
                message_type=message_type,
                content=content,
            ))
            session.commit()
    except Exception:
        logger.exception("chat_logger.log_outgoing failed — continuing")
