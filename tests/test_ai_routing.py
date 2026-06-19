"""Tests that each AI flow uses its assigned route + the fallback chain works.

OpenAI is mocked with a tiny fake client that records the model passed to
responses.create. get_model_for_route is stubbed to a per-route sentinel so the
assertions are unambiguous even when every real route resolves to the same model.
"""
from __future__ import annotations

import asyncio

import pytest

from app.services import ai_client
from app.services.model_router import ModelRoute


class _FakeResp:
    def __init__(self) -> None:
        self.output_text = "ok"
        self.status = "completed"
        self.usage = None


class _RecordingResponses:
    def __init__(self, calls: list[dict]) -> None:
        self._calls = calls

    async def create(self, **kwargs):
        self._calls.append(kwargs)
        return _FakeResp()


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.responses = _RecordingResponses(self.calls)


def _sentinel_router(route, settings_obj=None):
    value = route.value if isinstance(route, ModelRoute) else route
    return f"model-{value}"


@pytest.fixture
def fake_client(monkeypatch):
    fc = _FakeClient()
    monkeypatch.setattr(ai_client, "_client", fc)
    monkeypatch.setattr(ai_client, "get_model_for_route", _sentinel_router)
    return fc


def _run(coro):
    return asyncio.run(coro)


# --- each flow -> its route -------------------------------------------------

def test_daily_message_uses_coach_route(fake_client):
    _run(ai_client.generate_daily_message({"x": 1}, user_id=7))
    assert fake_client.calls[0]["model"] == "model-coach"


def test_qa_uses_coach_route(fake_client):
    _run(ai_client.generate_qa_response({"question": "hi"}, user_id=7))
    assert fake_client.calls[0]["model"] == "model-coach"


def test_focus_uses_coach_route(fake_client):
    _run(ai_client.generate_focus_response({"x": 1}, user_id=7))
    assert fake_client.calls[0]["model"] == "model-coach"


def test_weekly_uses_deep_route(fake_client):
    _run(ai_client.generate_weekly_message({"x": 1}, user_id=7))
    assert fake_client.calls[0]["model"] == "model-deep"


def test_classify_uses_fast_route(fake_client):
    _run(ai_client.classify_and_extract("had pizza", {"date": "2026-01-01"}, user_id=7))
    assert fake_client.calls[0]["model"] == "model-fast"


def test_extract_user_facts_uses_extract_route(fake_client):
    _run(ai_client.extract_user_facts("I take creatine", "noted", {}, user_id=7))
    assert fake_client.calls[0]["model"] == "model-extract"


# --- fallback chain ---------------------------------------------------------

def test_deep_falls_back_to_coach_then_succeeds(monkeypatch):
    monkeypatch.setattr(ai_client, "get_model_for_route", _sentinel_router)
    seen: list[str] = []

    class _FailFirstResponses:
        async def create(self, **kwargs):
            seen.append(kwargs["model"])
            if kwargs["model"] == "model-deep":
                raise RuntimeError("deep model unavailable")
            return _FakeResp()

    class _C:
        def __init__(self):
            self.responses = _FailFirstResponses()

    monkeypatch.setattr(ai_client, "_client", _C())
    out = _run(ai_client.generate_weekly_message({"x": 1}))
    assert out == "ok"
    assert seen[0] == "model-deep"
    assert seen[1] == "model-coach"  # degraded to coach model


def test_coach_falls_back_to_global_model(monkeypatch):
    monkeypatch.setattr(ai_client, "get_model_for_route", _sentinel_router)
    seen: list[str] = []
    global_model = ai_client.settings.openai_model

    class _FailFirstResponses:
        async def create(self, **kwargs):
            seen.append(kwargs["model"])
            if kwargs["model"] == "model-coach":
                raise RuntimeError("coach model unavailable")
            return _FakeResp()

    class _C:
        def __init__(self):
            self.responses = _FailFirstResponses()

    monkeypatch.setattr(ai_client, "_client", _C())
    out = _run(ai_client.generate_daily_message({"x": 1}))
    assert out == "ok"
    assert seen[0] == "model-coach"
    assert seen[1] == global_model  # last-resort global model


def test_all_models_failing_reraises(monkeypatch):
    monkeypatch.setattr(ai_client, "get_model_for_route", _sentinel_router)

    class _AlwaysFail:
        async def create(self, **kwargs):
            raise RuntimeError("everything down")

    class _C:
        def __init__(self):
            self.responses = _AlwaysFail()

    monkeypatch.setattr(ai_client, "_client", _C())
    with pytest.raises(RuntimeError, match="everything down"):
        _run(ai_client.generate_weekly_message({"x": 1}))


def test_fast_route_has_no_fallback_single_attempt(monkeypatch):
    # FAST/EXTRACT/QUALITY_GATE don't add fallback models — exactly one call.
    monkeypatch.setattr(ai_client, "get_model_for_route", _sentinel_router)
    seen: list[str] = []

    class _AlwaysFail:
        async def create(self, **kwargs):
            seen.append(kwargs["model"])
            raise RuntimeError("down")

    class _C:
        def __init__(self):
            self.responses = _AlwaysFail()

    monkeypatch.setattr(ai_client, "_client", _C())
    # classify_and_extract surfaces the raised error (caller handles it)
    with pytest.raises(RuntimeError):
        _run(ai_client.classify_and_extract("x", {}, user_id=1))
    assert seen == ["model-fast"]  # no retry, no fallback
