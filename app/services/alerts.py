"""Admin failure alerts. Silent pipeline death is the #1 project risk —
every job or auth failure must land in the admin's Telegram chat."""
from __future__ import annotations

import logging

from app.config import settings

logger = logging.getLogger(__name__)


async def send_admin_alert(text: str) -> None:
    """Best-effort: alerting must never crash the pipeline it's reporting on."""
    from app.telegram.bot import get_application

    try:
        application = get_application()
        await application.bot.send_message(
            chat_id=settings.admin_telegram_id, text=f"🚨 Body Manual alert:\n{text}"
        )
    except Exception:
        logger.exception("Failed to deliver admin alert: %s", text)
