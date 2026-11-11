"""AI provider abstraction layer and Gemini implementation using gemini-api-rotator."""

import json
import logging
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Literal

from gemini_rotator import GeminiAPIRotator, RotatorConfig
from gemini_rotator.adapters import MemoryAdapter
from gemini_rotator.middleware import ErrorType, classify_error

from app.ai.postgres_adapter import PostgresAdapter
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
    async def generate_json(
        self,
        prompt: str,
        system_instruction: str | None = None,
        *,
        purpose: Literal["analysis", "preflight", "schema_repair"] = "analysis",
    ) -> dict[str, Any]:
        """Generate structured JSON response for a given prompt, optionally with system instruction.

        Args:
            prompt: Text prompt to send to the model.
            system_instruction: Optional system instruction to guide model behaviour.
            purpose: Distinguishes call types for quota tracking and logging.
                - "analysis": regular article analysis (counts against quota)
                - "preflight": health check (separate counter)
                - "schema_repair": retry after Pydantic validation failure
        """
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
    """Google Gemini AI provider implementation using gemini-api-rotator with PostgreSQL persistence."""

    _total_calls: int = 0
    _preflight_calls: int = 0

    @staticmethod
    def _convert_dsn(dsn: str) -> str:
        """Convert SQLAlchemy async DSN to psycopg sync DSN.

        psycopg3 does not understand SQLAlchemy's '+asyncpg' driver marker.
        """
        return (
            dsn.replace("postgresql+asyncpg://", "postgresql://")
            .replace("postgres+asyncpg://", "postgresql://")
        )

    @classmethod
    def get_total_calls(cls) -> int:
        """Total calls made for actual article analysis."""
        return cls._total_calls

    @classmethod
    def get_preflight_calls(cls) -> int:
        """Total calls made for health/pre-flight checks."""
        return cls._preflight_calls

    @classmethod
    def reset_call_counters(cls) -> None:
        """Reset call counters (useful in tests and daily cycles)."""
        cls._total_calls = 0
        cls._preflight_calls = 0

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        rotator: GeminiAPIRotator | None = None,
        primary_model: str | None = None,
        fallback_model: str | None = None,
        enable_fallback: bool | None = None,
        key_db_path: str | Path | None = None,  # DEPRECATED: ignored; SQLite removed in F-1
        config: RotatorConfig | None = None,
    ):
        self._primary_model = primary_model or model or settings.gemini_primary_model
        self._fallback_model = fallback_model or settings.gemini_fallback_model
        self._enable_fallback = enable_fallback if enable_fallback is not None else settings.gemini_enable_fallback
        self.call_count: int = 0

        if self._enable_fallback and self._primary_model == self._fallback_model:
            raise ValueError(
                f"primary_model and fallback_model must differ when enable_fallback is True "
                f"(got '{self._primary_model}' for both)."
            )

        # Maintain model tracking: dynamically tracks the actual model that produced the response
        self._last_used_model = self._primary_model
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
                if settings.app_env == "test":
                    keys = ["mock_key_unconfigured"]
                else:
                    raise RuntimeError(
                        "No Gemini API keys configured. Set GEMINI_KEY_1..N or GEMINI_API_KEY in .env. "
                        "See docs/LOCAL_SETUP.md."
                    )

            # Adapter selection: MemoryAdapter in test (no DB dependency),
            # PostgresAdapter in dev/production (persistent state).
            # key_db_path parameter is DEPRECATED and ignored (SQLite removed in F-1).
            if settings.app_env == "test":
                self._db_adapter = MemoryAdapter()
            else:
                self._db_adapter = PostgresAdapter(
                    self._convert_dsn(settings.database_url)
                )

            rotator_config = config or RotatorConfig()
            self._rotator = GeminiAPIRotator(
                keys=keys,
                config=rotator_config,
                db=self._db_adapter,
            )

    @property
    def model_name(self) -> str:
        """Active model identifier conforming to ARCHITECTURE.md Section 3 (tracks actual model used)."""
        return self._last_used_model

    @property
    def primary_model(self) -> str:
        """Configured primary model identifier."""
        return self._primary_model

    @property
    def fallback_model(self) -> str:
        """Configured fallback model identifier."""
        return self._fallback_model

    @property
    def rotator(self) -> GeminiAPIRotator:
        """Underlying GeminiAPIRotator instance."""
        return self._rotator

    def close(self) -> None:
        """Close database adapter connection if present."""
        if self._db_adapter and hasattr(self._db_adapter, "close"):
            self._db_adapter.close()

    async def generate_json(
        self,
        prompt: str,
        system_instruction: str | None = None,
        *,
        purpose: Literal["analysis", "preflight", "schema_repair"] = "analysis",
    ) -> dict[str, Any]:
        """Generate structured JSON response using Gemini via gemini-api-rotator.

        Args:
            prompt: Text prompt to send to the model.
            system_instruction: Optional system instruction to guide model behaviour.
            purpose: Distinguishes call types for quota tracking and logging.
                - "analysis": regular article analysis (counts against quota)
                - "preflight": health check (separate counter)
                - "schema_repair": retry after Pydantic validation failure
        """
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
            self._last_used_model = target_model
            self._model = target_model
            latency = time.monotonic() - start_time

            if purpose == "preflight":
                GeminiProvider._preflight_calls += 1
                logger.info(
                    "Gemini PRE-FLIGHT check #%d completed: model=%s, latency=%.2fs",
                    GeminiProvider._preflight_calls,
                    target_model,
                    latency,
                )
            else:
                GeminiProvider._total_calls += 1
                self.call_count += 1
                logger.info(
                    "Gemini API call #%d completed [%s]: model=%s, prompt_len=%d, latency=%.2fs",
                    GeminiProvider._total_calls,
                    purpose,
                    target_model,
                    len(prompt),
                    latency,
                )
        except Exception as exc:
            # Fallback logic if primary model encounters retryable/transient/quota error
            if (
                is_retryable_ai_error(exc)
                and self._enable_fallback
                and self._fallback_model
                and target_model != self._fallback_model
            ):
                logger.warning(
                    "Primary model %s encountered retryable error (%s). Attempting fallback to %s.",
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
                    self._last_used_model = target_model
                    self._model = target_model
                    latency = time.monotonic() - start_time

                    if purpose == "preflight":
                        GeminiProvider._preflight_calls += 1
                        logger.info(
                            "Fallback Gemini PRE-FLIGHT check #%d completed: model=%s, latency=%.2fs",
                            GeminiProvider._preflight_calls,
                            target_model,
                            latency,
                        )
                    else:
                        GeminiProvider._total_calls += 1
                        self.call_count += 1
                        logger.info(
                            "Fallback Gemini API call #%d completed [%s]: model=%s, prompt_len=%d, latency=%.2fs",
                            GeminiProvider._total_calls,
                            purpose,
                            target_model,
                            len(prompt),
                            latency,
                        )
                except Exception as fb_exc:
                    self._last_used_model = self._primary_model
                    self._model = self._primary_model
                    _translate_and_raise(fb_exc)
            else:
                self._last_used_model = self._primary_model
                self._model = self._primary_model
                _translate_and_raise(exc)

        if not raw_text:
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
        self.calls_by_purpose: dict[str, int] = {
            "analysis": 0,
            "preflight": 0,
            "schema_repair": 0,
        }
        self.last_prompt: str | None = None
        self.last_system_instruction: str | None = None

    @property
    def call_count(self) -> int:
        return self.calls_by_purpose["analysis"]

    @call_count.setter
    def call_count(self, value: int) -> None:
        self.calls_by_purpose["analysis"] = value

    @property
    def preflight_count(self) -> int:
        return self.calls_by_purpose["preflight"]

    @preflight_count.setter
    def preflight_count(self, value: int) -> None:
        self.calls_by_purpose["preflight"] = value

    @property
    def model_name(self) -> str:
        return self._model

    def set_canned_response(self, response: dict[str, Any]) -> None:
        self._canned_response = response

    async def generate_json(
        self,
        prompt: str,
        system_instruction: str | None = None,
        *,
        purpose: Literal["analysis", "preflight", "schema_repair"] = "analysis",
    ) -> dict[str, Any]:
        """Generate structured JSON mock response.

        Args:
            prompt: Text prompt to send to the mock.
            system_instruction: Optional system instruction.
            purpose: Distinguishes call types for quota tracking and logging.
        """
        self.calls_by_purpose[purpose] = self.calls_by_purpose.get(purpose, 0) + 1
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
