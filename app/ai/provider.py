"""AI provider abstraction layer and Gemini implementation with retry, rate-limiting, and error handling."""

from abc import ABC, abstractmethod
import asyncio
import json
import logging
import time
from typing import Any

from google import genai
from google.genai import types
from google.genai.errors import APIError
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.config import settings

logger = logging.getLogger(__name__)


class AIProviderError(Exception):
    """Base exception for AI provider operations."""
    pass


class AIRateLimitError(AIProviderError):
    """Raised when rate limits are exceeded."""
    pass


class AIServiceUnavailableError(AIProviderError):
    """Raised when the AI service is temporarily unavailable."""
    pass


class BaseAIProvider(ABC):
    """Abstract base class defining the AI Provider interface (ARCHITECTURE.md Section 3)."""

    @abstractmethod
    async def generate_json(self, prompt: str, system_instruction: str | None = None) -> dict[str, Any]:
        """Generate structured JSON response for a given prompt, optionally with system instruction."""
        pass

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Name or identifier of the underlying AI model."""
        pass


def is_retryable_ai_error(exc: BaseException) -> bool:
    """Determine whether an API error is transient and should be retried."""
    if isinstance(exc, (AIRateLimitError, AIServiceUnavailableError)):
        return True
    if isinstance(exc, APIError):
        # Retry 429 (ResourceExhausted), 503 (Unavailable), 500 (Internal)
        code = getattr(exc, "code", None)
        if code in {429, 500, 502, 503, 504}:
            return True
        msg = str(exc).lower()
        if "resource_exhausted" in msg or "quota" in msg or "overloaded" in msg or "unavailable" in msg:
            return True
    return False


class GeminiProvider(BaseAIProvider):
    """Google Gemini AI provider implementation using google-genai SDK."""

    _total_calls: int = 0

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        max_rpm: int = 15,
        max_concurrency: int = 3,
        client: Any | None = None,
    ):
        self._api_key = api_key or settings.gemini_api_key
        self._model = model or settings.gemini_model
        self._max_rpm = max_rpm
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._request_timestamps: list[float] = []
        self._rate_limit_lock = asyncio.Lock()
        self.call_count: int = 0

        if client is not None:
            self._client = client
        elif self._api_key and not self._api_key.startswith("YOUR_"):
            self._client = genai.Client(api_key=self._api_key, http_options={"api_version": "v1beta"})
        else:
            self._client = None

    @property
    def model_name(self) -> str:
        return self._model

    async def _enforce_rate_limit(self) -> None:
        """Enforce requests per minute limit using a sliding window."""
        if self._max_rpm <= 0:
            return

        async with self._rate_limit_lock:
            now = time.monotonic()
            # Retain only timestamps within the last 60 seconds
            self._request_timestamps = [t for t in self._request_timestamps if now - t < 60.0]

            if len(self._request_timestamps) >= self._max_rpm:
                oldest = self._request_timestamps[0]
                sleep_needed = 60.0 - (now - oldest) + 0.1
                if sleep_needed > 0:
                    logger.warning("Gemini rate limit threshold reached (%d RPM), throttling for %.2fs", self._max_rpm, sleep_needed)
                    await asyncio.sleep(sleep_needed)
                    now = time.monotonic()
                    self._request_timestamps = [t for t in self._request_timestamps if now - t < 60.0]

            self._request_timestamps.append(time.monotonic())

    @retry(
        retry=retry_if_exception(is_retryable_ai_error),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    async def _call_api_with_retry(self, prompt: str, system_instruction: str | None = None) -> str:
        """Execute Gemini API call with exponential backoff on transient errors."""
        if not self._client:
            raise AIProviderError("Gemini API key is not configured or client is uninitialized.")

        await self._enforce_rate_limit()

        async with self._semaphore:
            start_time = time.monotonic()
            try:
                config_kwargs: dict[str, Any] = {
                    "response_mime_type": "application/json",
                    "temperature": 0.1,
                }
                if system_instruction:
                    config_kwargs["system_instruction"] = system_instruction

                response = await self._client.aio.models.generate_content(
                    model=self._model,
                    contents=prompt,
                    config=types.GenerateContentConfig(**config_kwargs),
                )
                latency = time.monotonic() - start_time
                GeminiProvider._total_calls += 1
                self.call_count += 1

                usage = getattr(response, "usage_metadata", None)
                tokens_in = getattr(usage, "prompt_token_count", None) if usage else None
                tokens_out = getattr(usage, "candidates_token_count", None) if usage else None
                logger.info(
                    "Gemini API call #%d completed: model=%s, prompt_len=%d, latency=%.2fs, tokens_in=%s, tokens_out=%s",
                    GeminiProvider._total_calls,
                    self._model,
                    len(prompt),
                    latency,
                    tokens_in,
                    tokens_out,
                )
                return response.text or "{}"
            except APIError as exc:
                code = getattr(exc, "code", None)
                msg = str(exc)
                if code == 429 or "resource_exhausted" in msg.lower():
                    logger.warning("Gemini rate limit exceeded (429): %s", msg)
                    raise AIRateLimitError(msg) from exc
                if code in {500, 502, 503, 504} or "unavailable" in msg.lower():
                    logger.warning("Gemini service unavailable (%s): %s", code, msg)
                    raise AIServiceUnavailableError(msg) from exc
                logger.error("Gemini API error (non-retryable): %s", msg)
                raise AIProviderError(f"Gemini API error: {msg}") from exc
            except Exception as exc:
                logger.error("Unexpected error during Gemini API call: %s", exc)
                raise AIProviderError(f"Unexpected error calling Gemini: {exc}") from exc

    async def generate_json(self, prompt: str, system_instruction: str | None = None) -> dict[str, Any]:
        """Generate structured JSON response using Gemini."""
        raw_text = await self._call_api_with_retry(prompt, system_instruction=system_instruction)
        cleaned = raw_text.strip()
        # Strip potential markdown fences if present
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        elif cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as exc:
            logger.error("Failed to parse JSON response from Gemini: %s (Raw text: %.200s)", exc, raw_text)
            raise AIProviderError(f"Invalid JSON returned by Gemini: {exc}") from exc


class MockAIProvider(BaseAIProvider):
    """Deterministic mock provider for unit testing and offline development."""

    def __init__(self, canned_response: dict[str, Any] | None = None, model: str = "mock-model-v1"):
        self._canned_response = canned_response or {}
        self._model = model
        self.call_count = 0
        self.last_prompt: str | None = None
        self.last_system_instruction: str | None = None

    @property
    def model_name(self) -> str:
        return self._model

    def set_canned_response(self, response: dict[str, Any]) -> None:
        self._canned_response = response

    async def generate_json(self, prompt: str, system_instruction: str | None = None) -> dict[str, Any]:
        self.call_count += 1
        self.last_prompt = prompt
        self.last_system_instruction = system_instruction
        return self._canned_response


def get_ai_provider() -> BaseAIProvider:
    """Factory to instantiate AI provider based on environment settings."""
    if (
        settings.app_env == "test"
        or not settings.gemini_api_key
        or settings.gemini_api_key.startswith("mock_")
        or settings.gemini_api_key.startswith("YOUR_")
    ):
        return MockAIProvider()
    return GeminiProvider()
