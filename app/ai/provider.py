"""AI provider abstraction layer and Gemini implementation using gemini-api-rotator."""

from abc import ABC, abstractmethod
import json
import logging
from pathlib import Path
import time
from typing import Any

from gemini_rotator import GeminiAPIRotator, RotatorConfig, SQLiteAdapter
from gemini_rotator.middleware import ErrorType, classify_error

from app.config import settings

logger = logging.getLogger(__name__)


class AIProviderError(Exception):
    """Base exception for AI provider operations."""
    pass


class AIRateLimitError(AIProviderError):
    """Raised when rate limits or quotas are exceeded."""
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
    if isinstance(exc, Exception):
        etype = classify_error(exc)
        if etype in (ErrorType.RATE_LIMIT, ErrorType.TRANSIENT):
            return True
    code = getattr(exc, "code", None)
    if code in {429, 500, 502, 503, 504}:
        return True
    msg = str(exc).lower()
    if "resource_exhausted" in msg or "quota" in msg or "overloaded" in msg or "unavailable" in msg:
        return True
    return False


def _is_rate_limit_or_quota(exc: Exception) -> bool:
    """Check if exception corresponds to quota exhaustion or rate limit."""
    if isinstance(exc, AIRateLimitError):
        return True
    if classify_error(exc) == ErrorType.RATE_LIMIT:
        return True
    msg = str(exc).lower()
    if "429" in msg or "resource_exhausted" in msg or "quota" in msg or "all keys are suspended" in msg or "no api keys available" in msg:
        return True
    code = getattr(exc, "code", None)
    return code == 429


def _translate_and_raise(exc: Exception) -> None:
    """Translate raw library / SDK exceptions to middle-east-news-bot domain exceptions."""
    if isinstance(exc, AIProviderError):
        raise exc
    if _is_rate_limit_or_quota(exc):
        raise AIRateLimitError(str(exc)) from exc
    if classify_error(exc) == ErrorType.TRANSIENT:
        raise AIServiceUnavailableError(str(exc)) from exc
    code = getattr(exc, "code", None)
    msg = str(exc)
    if code in {500, 502, 503, 504} or "unavailable" in msg.lower() or "internal" in msg.lower():
        raise AIServiceUnavailableError(str(exc)) from exc
    raise AIProviderError(f"Gemini API error: {exc}") from exc


class GeminiProvider(BaseAIProvider):
    """Google Gemini AI provider implementation using gemini-api-rotator with SQLite persistence."""

    _total_calls: int = 0

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        rotator: GeminiAPIRotator | None = None,
        primary_model: str | None = None,
        fallback_model: str | None = None,
        enable_fallback: bool | None = None,
        key_db_path: str | Path | None = None,
        config: RotatorConfig | None = None,
        **kwargs: Any,  # Absorb legacy kwargs (max_rpm, max_concurrency, client, etc.)
    ):
        self._primary_model = primary_model or model or settings.gemini_primary_model
        self._fallback_model = fallback_model or settings.gemini_fallback_model
        self._enable_fallback = enable_fallback if enable_fallback is not None else settings.gemini_enable_fallback
        self.call_count: int = 0

        # Maintain legacy attribute alias
        self._model = self._primary_model

        if rotator is not None:
            self._rotator = rotator
            self._db_adapter = getattr(rotator, "_db", None)
        else:
            keys: list[str] = []
            if api_key:
                keys = [api_key]
            else:
                keys = settings.get_gemini_keys()

            if not keys:
                keys = ["mock_key_unconfigured"]

            db_path_str = str(key_db_path or settings.gemini_key_db_path)
            Path(db_path_str).parent.mkdir(parents=True, exist_ok=True)
            self._db_adapter = SQLiteAdapter(db_path_str)

            rotator_config = config or RotatorConfig()
            self._rotator = GeminiAPIRotator(
                keys=keys,
                config=rotator_config,
                db=self._db_adapter,
            )

    @property
    def model_name(self) -> str:
        """Active primary model identifier conforming to ARCHITECTURE.md Section 3."""
        return self._primary_model

    @property
    def rotator(self) -> GeminiAPIRotator:
        """Underlying GeminiAPIRotator instance."""
        return self._rotator

    def close(self) -> None:
        """Close SQLiteAdapter connection if present."""
        if self._db_adapter and hasattr(self._db_adapter, "close"):
            self._db_adapter.close()

    async def _enforce_rate_limit(self) -> None:
        """Deprecated legacy rate limiter hook retained for backward compatibility."""
        pass

    async def generate_json(self, prompt: str, system_instruction: str | None = None) -> dict[str, Any]:
        """Generate structured JSON response using Gemini via gemini-api-rotator."""
        extra_config: dict[str, Any] = {
            "response_mime_type": "application/json",
            "temperature": 0.1,
        }

        target_model = self._primary_model
        start_time = time.monotonic()
        raw_text: str | None = None

        try:
            response = await self._rotator.execute(
                prompt,
                model=target_model,
                system=system_instruction,
                extra_config=extra_config,
            )
            raw_text = getattr(response, "text", "") or ""
            GeminiProvider._total_calls += 1
            self.call_count += 1
            latency = time.monotonic() - start_time
            logger.info(
                "Gemini API call #%d completed: model=%s, prompt_len=%d, latency=%.2fs",
                GeminiProvider._total_calls,
                target_model,
                len(prompt),
                latency,
            )
        except Exception as exc:
            # Fallback logic if primary model is rate-limited / quota-exhausted
            if (
                _is_rate_limit_or_quota(exc)
                and self._enable_fallback
                and self._fallback_model
                and target_model != self._fallback_model
            ):
                logger.warning(
                    "Primary model %s rate-limited/exhausted (%s). Attempting fallback to %s.",
                    target_model,
                    exc,
                    self._fallback_model,
                )
                try:
                    target_model = self._fallback_model
                    response = await self._rotator.execute(
                        prompt,
                        model=target_model,
                        system=system_instruction,
                        extra_config=extra_config,
                    )
                    raw_text = getattr(response, "text", "") or ""
                    GeminiProvider._total_calls += 1
                    self.call_count += 1
                    latency = time.monotonic() - start_time
                    logger.info(
                        "Fallback Gemini API call #%d completed: model=%s, prompt_len=%d, latency=%.2fs",
                        GeminiProvider._total_calls,
                        target_model,
                        len(prompt),
                        latency,
                    )
                except Exception as fb_exc:
                    _translate_and_raise(fb_exc)
            else:
                _translate_and_raise(exc)

        if raw_text is None:
            raw_text = "{}"

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
    keys = settings.get_gemini_keys()
    has_real_key = any(
        k and not k.startswith("mock_") and not k.startswith("YOUR_")
        for k in keys
    )
    if settings.app_env == "test" or not has_real_key:
        return MockAIProvider()
    return GeminiProvider()
