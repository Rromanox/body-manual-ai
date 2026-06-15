from __future__ import annotations

from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from app.config import settings
from app.telegram import handlers

_application: Application | None = None


def build_application() -> Application:
    global _application
    application = Application.builder().token(settings.telegram_bot_token).updater(None).build()

    application.add_handler(CommandHandler("start", handlers.start))
    application.add_handler(CommandHandler("connect_whoop", handlers.connect_whoop))
    application.add_handler(CommandHandler("today", handlers.today))
    application.add_handler(CommandHandler("checkin", handlers.checkin))
    application.add_handler(CommandHandler("weekly", handlers.weekly))
    application.add_handler(CommandHandler("manual", handlers.manual))
    application.add_handler(CommandHandler("backfill", handlers.backfill))
    application.add_handler(CommandHandler("delete", handlers.delete))

    application.add_handler(CallbackQueryHandler(handlers.checkin_callback, pattern=r"^ci_"))
    application.add_handler(CallbackQueryHandler(handlers.delete_callback, pattern=r"^del_"))

    # Any non-command text is a question to the coach
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.plain_text))

    _application = application
    return application


def get_application() -> Application:
    if _application is None:
        raise RuntimeError("Telegram application is not initialized yet")
    return _application
