from __future__ import annotations

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from app.config import settings
from app.telegram import handlers

_application: Application | None = None


async def _log_all_incoming(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Group -1 handler: log every incoming message before the real handlers run."""
    from app.services.chat_logger import log_incoming
    from sqlalchemy import select as _select
    from app.db import SessionLocal as _SL
    from app.models.user import User as _User

    if update.effective_user is None:
        return
    tg_id = update.effective_user.id

    user_id: int | None = None
    try:
        with _SL() as s:
            u = s.scalar(_select(_User).where(_User.telegram_id == tg_id))
            if u:
                user_id = u.id
    except Exception:
        pass

    if update.message and update.message.text:
        text = update.message.text
        mtype = "command" if text.startswith("/") else "q_and_a"
        log_incoming(tg_id, text, mtype, user_id=user_id)
    elif update.callback_query and update.callback_query.data:
        log_incoming(tg_id, update.callback_query.data, "checkin", user_id=user_id)


def build_application() -> Application:
    global _application
    application = Application.builder().token(settings.telegram_bot_token).updater(None).build()

    # Group -1: runs before all handlers, logs every incoming message
    application.add_handler(
        MessageHandler(filters.ALL, _log_all_incoming), group=-1
    )
    application.add_handler(
        CallbackQueryHandler(_log_all_incoming), group=-1
    )

    application.add_handler(CommandHandler("start", handlers.start))
    application.add_handler(CommandHandler("connect_whoop", handlers.connect_whoop))
    application.add_handler(CommandHandler("connect_withings", handlers.connect_withings))
    application.add_handler(CommandHandler("today", handlers.today))
    application.add_handler(CommandHandler("checkin", handlers.checkin))
    application.add_handler(CommandHandler("weekly", handlers.weekly))
    application.add_handler(CommandHandler("manual", handlers.manual))
    application.add_handler(CommandHandler("goal", handlers.goal))
    application.add_handler(CommandHandler("history", handlers.history))
    application.add_handler(CommandHandler("focus", handlers.focus))
    application.add_handler(CommandHandler("experiment", handlers.experiment))
    application.add_handler(CommandHandler("chatlog", handlers.chatlog))
    application.add_handler(CommandHandler("backfill", handlers.backfill))
    application.add_handler(CommandHandler("delete", handlers.delete))

    application.add_handler(CallbackQueryHandler(handlers.checkin_callback, pattern=r"^ci_"))
    application.add_handler(CallbackQueryHandler(handlers.delete_callback, pattern=r"^del_"))
    application.add_handler(CallbackQueryHandler(handlers.goal_callback, pattern=r"^goal:"))

    # Any non-command text is a question to the coach
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.plain_text))

    _application = application
    return application


def get_application() -> Application:
    if _application is None:
        raise RuntimeError("Telegram application is not initialized yet")
    return _application
