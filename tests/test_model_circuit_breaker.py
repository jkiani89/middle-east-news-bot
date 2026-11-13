"""Phase Q: model-level circuit breaker, single retry and per-call timeout."""

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.ai.provider import AIProviderError, GeminiProvider
from app.config import settings

PRIMARY = "gemini-primary"
FALLBACK = "gemini-fallback"


class FakeRotator:
    """Records the model of every call and fails the models listed in ``failing``."""

    def __init__(self, failing: set[str] | None = None, delay: float = 0.0):
        self.calls: list[str] = []
        self.failing = failing or set()
        self.delay = delay

    async def execute(self, prompt: str, model: str | None = None, system: Any = None, extra_config: Any = None):
        self.calls.append(model or "")
        if self.delay:
            await asyncio.sleep(self.delay)
        if model in self.failing:
            raise Exception("503 UNAVAILABLE. This model is currently experiencing high demand.")
        return SimpleNamespace(text=json.dumps({"ok": True}))


def _provider(rotator: FakeRotator, timeout: float = 30.0) -> GeminiProvider:
    provider = GeminiProvider(
        rotator=rotator,  # type: ignore[arg-type]
        primary_model=PRIMARY,
        fallback_model=FALLBACK,
        enable_fallback=True,
    )
    provider._request_timeout = timeout
    return provider


@pytest.mark.asyncio
async def test_primary_model_is_used_while_healthy():
    rotator = FakeRotator()
    provider = _provider(rotator)

    assert await provider.generate_json("prompt") == {"ok": True}
    assert rotator.calls == [PRIMARY]
    assert provider.primary_disabled is False


@pytest.mark.asyncio
async def test_circuit_breaker_skips_primary_after_two_failures():
    rotator = FakeRotator(failing={PRIMARY})
    provider = _provider(rotator)

    for _ in range(3):
        assert await provider.generate_json("prompt") == {"ok": True}

    # Two primary attempts, each rescued by the fallback; the third call goes
    # straight to the fallback without touching the failed primary model.
    assert rotator.calls == [PRIMARY, FALLBACK, PRIMARY, FALLBACK, FALLBACK]
    assert provider.primary_disabled is True
    assert provider.model_name == FALLBACK


@pytest.mark.asyncio
async def test_circuit_breaker_does_not_trip_on_single_failure():
    rotator = FakeRotator(failing={PRIMARY})
    provider = _provider(rotator)

    await provider.generate_json("prompt")
    assert provider.primary_disabled is False

    rotator.failing = set()
    await provider.generate_json("prompt")
    assert rotator.calls == [PRIMARY, FALLBACK, PRIMARY]


@pytest.mark.asyncio
async def test_slow_primary_call_times_out_and_falls_back():
    rotator = FakeRotator(delay=0.5)
    provider = _provider(rotator, timeout=0.05)

    # Both models are slow here, so the timeout surfaces as a provider error,
    # but the important part is that no call may hang for the SDK's own retries.
    with pytest.raises(AIProviderError):
        await provider.generate_json("prompt")

    assert rotator.calls == [PRIMARY, FALLBACK]


@pytest.mark.asyncio
async def test_fallback_recovers_when_primary_is_unreachable():
    rotator = FakeRotator(failing={PRIMARY})
    provider = _provider(rotator)

    assert await provider.generate_json("prompt") == {"ok": True}
    assert rotator.calls == [PRIMARY, FALLBACK]


def test_rotator_configured_with_single_retry():
    """A second attempt against a dead model only adds latency."""
    provider = GeminiProvider(primary_model=PRIMARY, fallback_model=FALLBACK)
    try:
        assert provider.rotator.config.max_retries == 1
    finally:
        provider.close()


def test_request_timeout_defaults_to_30_seconds():
    assert settings.ai_request_timeout_seconds == 30
