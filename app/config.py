"""Environment-backed configuration. Everything comes from .env / process env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _fix_db_url(url: str) -> str:
    # Railway (and some other hosts) provide postgresql:// but SQLAlchemy
    # requires the explicit driver scheme postgresql+psycopg2://
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


# The model used when neither a route-specific nor the global env var is set.
# This is the only place the default model name is hard-coded.
_DEFAULT_OPENAI_MODEL = "gpt-4o-mini"


def _global_openai_model() -> str:
    return os.getenv("OPENAI_MODEL", _DEFAULT_OPENAI_MODEL)


def _route_model(env_var: str) -> str:
    """Resolve a per-route model: route-specific env var, else the global model.

    An unset OR empty route var falls back to OPENAI_MODEL, which itself falls
    back to _DEFAULT_OPENAI_MODEL — so with no new env vars set, every route
    uses exactly today's model and behavior is unchanged.
    """
    return os.getenv(env_var) or _global_openai_model()


def parse_hhmm_to_minutes(value: str | None, default: str) -> int:
    """Parse "HH:MM" local time into minutes-since-midnight; fall back to default.

    Used for the wake-aware morning watch window. Invalid/blank input falls back
    to the supplied default so a bad env var can never crash startup.
    """
    for candidate in (value, default):
        if not candidate:
            continue
        try:
            hh, mm = candidate.split(":")
            h, m = int(hh), int(mm)
            if 0 <= h < 24 and 0 <= m < 60:
                return h * 60 + m
        except (ValueError, AttributeError):
            continue
    # default is a trusted constant; this line is effectively unreachable
    h, m = default.split(":")
    return int(h) * 60 + int(m)


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    admin_telegram_id: int = field(default_factory=lambda: int(os.getenv("ADMIN_TELEGRAM_ID") or "0"))
    database_url: str = field(
        default_factory=lambda: _fix_db_url(
            os.getenv("DATABASE_URL", "postgresql+psycopg2://postgres:postgres@localhost:5432/bodymanual")
        )
    )
    whoop_client_id: str = field(default_factory=lambda: os.getenv("WHOOP_CLIENT_ID", ""))
    whoop_client_secret: str = field(default_factory=lambda: os.getenv("WHOOP_CLIENT_SECRET", ""))
    withings_client_id: str = field(default_factory=lambda: os.getenv("WITHINGS_CLIENT_ID", ""))
    withings_client_secret: str = field(default_factory=lambda: os.getenv("WITHINGS_CLIENT_SECRET", ""))
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    # Global model + per-route overrides. Each route falls back to openai_model
    # when its own env var is unset, so existing deployments keep working with
    # only OPENAI_MODEL set. See app/services/model_router.py for what each
    # route is used for. Model names are NEVER hard-coded outside config.
    openai_model: str = field(default_factory=_global_openai_model)
    openai_model_fast: str = field(default_factory=lambda: _route_model("OPENAI_MODEL_FAST"))
    openai_model_extract: str = field(default_factory=lambda: _route_model("OPENAI_MODEL_EXTRACT"))
    openai_model_coach: str = field(default_factory=lambda: _route_model("OPENAI_MODEL_COACH"))
    openai_model_deep: str = field(default_factory=lambda: _route_model("OPENAI_MODEL_DEEP"))
    openai_model_quality_gate: str = field(default_factory=lambda: _route_model("OPENAI_MODEL_QUALITY_GATE"))
    base_url: str = field(default_factory=lambda: os.getenv("BASE_URL", "http://localhost:8000").rstrip("/"))
    secret_key: str = field(default_factory=lambda: os.getenv("SECRET_KEY", ""))
    # IANA timezone NAME (never a fixed offset) so zoneinfo handles DST. New
    # users default to this; it's also the scheduler's reference tz.
    default_timezone: str = field(default_factory=lambda: os.getenv("DEFAULT_TIMEZONE", "America/Detroit"))
    daily_pull_hour: int = field(default_factory=lambda: int(os.getenv("DAILY_PULL_HOUR", "6")))
    # SPEC §8: weekly summary "sent Sunday evening" — local hour, same gating
    # style as daily_pull_hour.
    weekly_send_hour: int = field(default_factory=lambda: int(os.getenv("WEEKLY_SEND_HOUR", "18")))
    # Wake-aware morning message: poll WHOOP across this LOCAL window and send once
    # the main sleep has ended and recovery/sleep is usable; at the cutoff, send a
    # degraded fallback. Stored as minutes-since-midnight for easy comparison.
    morning_watch_start_minutes: int = field(
        default_factory=lambda: parse_hhmm_to_minutes(os.getenv("MORNING_WATCH_START_LOCAL"), "05:00")
    )
    morning_watch_cutoff_minutes: int = field(
        default_factory=lambda: parse_hhmm_to_minutes(os.getenv("MORNING_WATCH_CUTOFF_LOCAL"), "10:30")
    )
    morning_watch_interval_minutes: int = field(
        default_factory=lambda: int(os.getenv("MORNING_WATCH_INTERVAL_MINUTES", "30"))
    )


settings = Settings()

# WHOOP credentials are not required at startup — the app boots and serves the
# OAuth callback URL without them. They are only needed when a user runs
# /connect_whoop, at which point whoop_client.py raises a clear error.
_REQUIRED_FOR_STARTUP = (
    "telegram_bot_token",
    "admin_telegram_id",
    "openai_api_key",
    "secret_key",
)


def validate_startup_settings() -> None:
    missing = [name for name in _REQUIRED_FOR_STARTUP if not getattr(settings, name)]
    if missing:
        raise RuntimeError(
            "Missing required settings (set them in .env): " + ", ".join(name.upper() for name in missing)
        )
