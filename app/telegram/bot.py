from __future__ import annotations

from telegram.ext import Application, CommandHandler, MessageHandler, filters

from app.config import settings
from app.telegram import handlers

_application: Application | None = None


def build_application() -> Application:
    global _application
    # updater(None) disables long-polling; we receive updates via webhook instead
    application = Application.builder().token(settings.telegram_bot_token).updater(None).build()
    application.add_handler(CommandHandler("start", handlers.start))
    application.add_handler(CommandHandler("connect_whoop", handlers.connect_whoop))
    application.add_handler(CommandHandler("today", handlers.today))
    # No /ask command — any non-command text is a question to the coach
    # (Q&A handler itself ships in Week 2; until then a stub reply).
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.plain_text))
    _application = application
    return application


def get_application() -> Application:
    if _application is None:
        raise RuntimeError("Telegram application is not initialized yet")
    return _application
