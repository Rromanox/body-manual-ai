"""Environment-backed configuration. Everything comes from .env / process env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    admin_telegram_id: int = field(default_factory=lambda: int(os.getenv("ADMIN_TELEGRAM_ID", "0")))
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL", "postgresql+psycopg2://postgres:postgres@localhost:5432/bodymanual"
        )
    )
    whoop_client_id: str = field(default_factory=lambda: os.getenv("WHOOP_CLIENT_ID", ""))
    whoop_client_secret: str = field(default_factory=lambda: os.getenv("WHOOP_CLIENT_SECRET", ""))
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    openai_model: str = field(default_factory=lambda: os.getenv("OPENAI_MODEL", "gpt-5.5-mini"))
    base_url: str = field(default_factory=lambda: os.getenv("BASE_URL", "http://localhost:8000").rstrip("/"))
    secret_key: str = field(default_factory=lambda: os.getenv("SECRET_KEY", ""))
    default_timezone: str = field(default_factory=lambda: os.getenv("DEFAULT_TIMEZONE", "UTC"))
    daily_pull_hour: int = field(default_factory=lambda: int(os.getenv("DAILY_PULL_HOUR", "6")))


settings = Settings()

_REQUIRED_FOR_STARTUP = (
    "telegram_bot_token",
    "admin_telegram_id",
    "whoop_client_id",
    "whoop_client_secret",
    "openai_api_key",
    "secret_key",
)


def validate_startup_settings() -> None:
    missing = [name for name in _REQUIRED_FOR_STARTUP if not getattr(settings, name)]
    if missing:
        raise RuntimeError(
            "Missing required settings (set them in .env): " + ", ".join(name.upper() for name in missing)
        )
