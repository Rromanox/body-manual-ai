"""Model routing: map a named task route to a configured OpenAI model.

The app uses different model tiers for different jobs without hard-coding model
names anywhere but config. Routes:

- FAST          cheap/quick classification (log-vs-question, intent, yes/no checks)
- EXTRACT       structured JSON extraction (events, user facts, future memory/
                recommendation extraction)
- COACH         user-facing coaching prose (morning /today, Q&A, /focus)
- DEEP          higher-value synthesis (weekly summary, future rule generation,
                recommendation outcome analysis, memory review, /manual rebuild)
- QUALITY_GATE  cheap response validation (future anti-generic advice check)

Every route resolves to a model via settings; with no per-route env vars set,
all routes resolve to OPENAI_MODEL (today's behavior). To change a tier, set the
matching env var (e.g. OPENAI_MODEL_DEEP) — see .env.example. Nothing else in
the codebase should read model names directly.
"""
from __future__ import annotations

from enum import Enum

from app.config import Settings, settings


class ModelRoute(str, Enum):
    FAST = "fast"
    EXTRACT = "extract"
    COACH = "coach"
    DEEP = "deep"
    QUALITY_GATE = "quality_gate"


# Route -> the Settings attribute holding that route's model name.
_ROUTE_TO_FIELD: dict[ModelRoute, str] = {
    ModelRoute.FAST: "openai_model_fast",
    ModelRoute.EXTRACT: "openai_model_extract",
    ModelRoute.COACH: "openai_model_coach",
    ModelRoute.DEEP: "openai_model_deep",
    ModelRoute.QUALITY_GATE: "openai_model_quality_gate",
}


def get_model_for_route(
    route: ModelRoute | str,
    settings_obj: Settings | None = None,
) -> str:
    """Return the configured model name for a route.

    Accepts a ModelRoute or its string value ("coach"). Raises ValueError with a
    clear message for an unknown route. ``settings_obj`` is for tests; production
    callers use the module-level settings singleton.
    """
    try:
        resolved = route if isinstance(route, ModelRoute) else ModelRoute(route)
    except ValueError as exc:
        valid = ", ".join(r.value for r in ModelRoute)
        raise ValueError(f"Unknown model route {route!r}. Valid routes: {valid}") from exc
    source = settings_obj if settings_obj is not None else settings
    return getattr(source, _ROUTE_TO_FIELD[resolved])
