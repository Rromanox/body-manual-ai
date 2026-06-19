"""Tests for model routing config + resolution (model-routing foundation)."""
from __future__ import annotations

import pytest

from app.config import Settings
from app.services.model_router import ModelRoute, get_model_for_route

_ALL_ROUTE_ENV = [
    "OPENAI_MODEL",
    "OPENAI_MODEL_FAST",
    "OPENAI_MODEL_EXTRACT",
    "OPENAI_MODEL_COACH",
    "OPENAI_MODEL_DEEP",
    "OPENAI_MODEL_QUALITY_GATE",
]


def _clear(monkeypatch):
    for var in _ALL_ROUTE_ENV:
        monkeypatch.delenv(var, raising=False)


# --- fallback chain: route var -> OPENAI_MODEL -> built-in default -----------

def test_no_env_vars_preserves_default(monkeypatch):
    _clear(monkeypatch)
    s = Settings()
    for route in ModelRoute:
        assert get_model_for_route(route, settings_obj=s) == "gpt-4o-mini"


def test_global_override_applies_to_all_routes(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o")
    s = Settings()
    for route in ModelRoute:
        assert get_model_for_route(route, settings_obj=s) == "gpt-4o"


def test_route_override_only_affects_that_route(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("OPENAI_MODEL_COACH", "gpt-4o")
    monkeypatch.setenv("OPENAI_MODEL_DEEP", "o1-preview")
    s = Settings()
    assert get_model_for_route(ModelRoute.COACH, settings_obj=s) == "gpt-4o"
    assert get_model_for_route(ModelRoute.DEEP, settings_obj=s) == "o1-preview"
    # untouched routes still resolve to the global model
    assert get_model_for_route(ModelRoute.FAST, settings_obj=s) == "gpt-4o-mini"
    assert get_model_for_route(ModelRoute.EXTRACT, settings_obj=s) == "gpt-4o-mini"
    assert get_model_for_route(ModelRoute.QUALITY_GATE, settings_obj=s) == "gpt-4o-mini"


def test_empty_route_var_falls_back_to_global(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "")  # explicitly blank
    s = Settings()
    assert get_model_for_route(ModelRoute.FAST, settings_obj=s) == "gpt-4o"


# --- resolver input handling ------------------------------------------------

def test_string_route_is_accepted(monkeypatch):
    _clear(monkeypatch)
    s = Settings()
    assert get_model_for_route("coach", settings_obj=s) == "gpt-4o-mini"


def test_invalid_route_raises_clear_error():
    with pytest.raises(ValueError) as exc:
        get_model_for_route("nonsense")
    assert "Unknown model route" in str(exc.value)


def test_every_route_is_mapped():
    # Guard against adding a ModelRoute without a settings field mapping.
    s = Settings()
    for route in ModelRoute:
        assert isinstance(get_model_for_route(route, settings_obj=s), str)
