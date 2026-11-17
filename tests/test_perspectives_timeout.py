"""Perspective calls get their own timeout and at most one timeout-only retry."""

from __future__ import annotations

import pytest

from app.ai.provider import AIProviderError, GeminiProvider
from app.config import Settings, settings


class _Response:
    text = '{"ok": true}'


class _Rotator:
    """Minimal stand-in for GeminiAPIRotator that replays scripted outcomes."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[str] = []

    async def execute(self, prompt, model=None, system=None, extra_config=None):
        self.calls.append(model)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _provider(outcomes, *, enable_fallback: bool = False) -> GeminiProvider:
    return GeminiProvider(rotator=_Rotator(outcomes), enable_fallback=enable_fallback)


def test_perspectives_use_their_own_ceiling():
    provider = _provider([])

    assert provider._timeout_for("analysis") == float(settings.ai_request_timeout_seconds)
    assert provider._timeout_for("perspectives") == float(
        settings.ai_perspectives_timeout_seconds
    )
    assert provider._timeout_for("perspectives") > provider._timeout_for("analysis")


@pytest.mark.asyncio
async def test_retries_stay_capped_even_if_the_setting_is_mutated(monkeypatch):
    monkeypatch.setattr(settings, "ai_perspectives_retries", 5)
    rotator = _Rotator([TimeoutError(), TimeoutError(), TimeoutError(), _Response()])
    provider = GeminiProvider(rotator=rotator, enable_fallback=False)

    with pytest.raises(AIProviderError):
        await provider.generate_json("prompt", purpose="perspectives")

    assert len(rotator.calls) == 2  # one attempt plus at most one retry


def test_perspective_retries_are_clamped_to_one():
    assert Settings(_env_file=None, ai_perspectives_retries=7).ai_perspectives_retries == 1
    assert Settings(_env_file=None, ai_perspectives_retries=-3).ai_perspectives_retries == 0


@pytest.mark.asyncio
async def test_perspectives_retry_once_after_a_timeout(monkeypatch):
    monkeypatch.setattr(settings, "ai_perspectives_retries", 1)
    rotator = _Rotator([TimeoutError(), _Response()])
    provider = GeminiProvider(rotator=rotator)

    result = await provider.generate_json("prompt", purpose="perspectives")

    assert result == {"ok": True}
    assert len(rotator.calls) == 2


@pytest.mark.asyncio
async def test_a_final_perspective_timeout_is_still_raised(monkeypatch):
    monkeypatch.setattr(settings, "ai_perspectives_retries", 1)
    rotator = _Rotator([TimeoutError(), TimeoutError()])
    provider = GeminiProvider(rotator=rotator, enable_fallback=False)

    with pytest.raises(AIProviderError):
        await provider.generate_json("prompt", purpose="perspectives")

    assert len(rotator.calls) == 2  # no third attempt, no swallowed error


@pytest.mark.asyncio
async def test_analysis_is_not_retried_after_a_timeout():
    rotator = _Rotator([TimeoutError(), _Response()])
    provider = GeminiProvider(rotator=rotator, enable_fallback=False)

    with pytest.raises(AIProviderError):
        await provider.generate_json("prompt", purpose="analysis")

    assert len(rotator.calls) == 1
