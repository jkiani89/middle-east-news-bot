"""AI provider abstraction layer and Gemini implementation using gemini-api-rotator."""

import asyncio
import json
import logging
import queue
import threading
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

# google-genai >= 3.0 defaults the Gemini Developer API to v1, and v1 rejects
# system_instruction ("Developer instruction is not enabled for api version v1").
# gemini_rotator builds genai.Client without http_options, so the version is pinned
# per request through GenerateContentConfig.http_options for every model in the chain.
GEMINI_API_VERSION = "v1beta"

# gemini_usage rows used to be written with ``asyncio.to_thread``, i.e. on the same
# default executor that runs the blocking google-genai calls. A call that timed out was
# only detached, its worker thread stayed alive inside the SDK, and the audit insert of
# the *next* successful call queued behind it: one article then took minutes although
# the model had answered in seconds (the 171-second gaps between consecutive articles in
# run #237). An audit row is never needed synchronously, so it goes to one daemon worker
# thread instead of the shared pool.
_AUDIT_QUEUE: queue.Queue[tuple[Any, dict[str, Any]] | None] = queue.Queue()
_AUDIT_THREAD: threading.Thread | None = None
_AUDIT_THREAD_LOCK = threading.Lock()


def _audit_worker() -> None:
    """Write queued gemini_usage rows one at a time; never raise into the caller."""
    while True:
        item = _AUDIT_QUEUE.get()
        try:
            if item is None:
                return
            func, kwargs = item
            func(**kwargs)
        except Exception:  # noqa: BLE001 - audit must not break the AI pipeline
            logger.exception("Failed to record gemini_usage row; continuing.")
        finally:
            _AUDIT_QUEUE.task_done()


def _ensure_audit_worker() -> None:
    """Start the audit writer on first use; the thread is a daemon on purpose.

    A non-daemon thread would be joined at interpreter exit, which is exactly the
    end-of-run hang this module avoids: ``scripts/run_once.py`` leaves with
    ``os._exit`` and must never wait for a pending audit insert.
    """
    global _AUDIT_THREAD
    if _AUDIT_THREAD is not None:
        return
    with _AUDIT_THREAD_LOCK:
        if _AUDIT_THREAD is None:
            thread = threading.Thread(
                target=_audit_worker,
                name="gemini-usage-audit",
                daemon=True,
            )
            thread.start()
            _AUDIT_THREAD = thread


def flush_gemini_audit(timeout_seconds: float = 5.0) -> int:
    """Wait at most ``timeout_seconds`` for queued audit rows; returns rows still queued."""
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while _AUDIT_QUEUE.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.02)
    return _AUDIT_QUEUE.unfinished_tasks


class AIProviderError(Exception):
    """Base exception for AI provider operations.

    ``raw_length`` carries the size of the raw model text when the failure came from
    unparseable JSON, so callers can refuse an expensive repair round-trip without
    needing the raw body itself (Phase Y-2).
    """

    def __init__(self, message: str, *, raw_length: int = 0) -> None:
        super().__init__(message)
        self.raw_length = raw_length


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
        purpose: Literal["analysis", "preflight", "schema_repair", "perspectives"] = "analysis",
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
    if isinstance(exc, (AIRateLimitError, AIServiceUnavailableError, TimeoutError)):
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

    # After this many consecutive primary-model failures the provider stops trying it
    # for the rest of the process (one process == one batch run). A free-tier Flash
    # model answers 503 for hours and burns ~2 minutes per article before falling back.
    PRIMARY_FAILURE_THRESHOLD: int = 2

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
        tertiary_model: str | None = None,
        enable_fallback: bool | None = None,
        key_db_path: str | Path | None = None,  # DEPRECATED: ignored; SQLite removed in F-1
        config: RotatorConfig | None = None,
    ):
        self._primary_model = primary_model or model or settings.gemini_primary_model
        self._fallback_model = fallback_model or settings.gemini_fallback_model
        self._tertiary_model = (
            tertiary_model if tertiary_model is not None else settings.gemini_tertiary_model
        )
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

        # Model-level circuit breaker state (reset with every new provider instance).
        self._primary_failures: int = 0
        self._primary_disabled: bool = False
        self._request_timeout: float = float(settings.ai_request_timeout_seconds)

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

            # One retry: a second attempt against an unavailable model only adds latency,
            # the model-level circuit breaker and fallback cover recovery instead.
            rotator_config = config or RotatorConfig(max_retries=1)
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
    def tertiary_model(self) -> str:
        """Configured last-resort fallback model identifier."""
        return self._tertiary_model

    def _model_chain(self) -> list[str]:
        """Models to try, in order: primary, fallback, then the tertiary last resort.

        Duplicates and empty names are dropped, so a tertiary model equal to the fallback
        never causes a second identical attempt with the same failing capacity.
        """
        if not self._enable_fallback or not self._fallback_model:
            return [self._primary_model]
        chain = [self._primary_model]
        for candidate in (self._fallback_model, self._tertiary_model):
            if candidate and candidate not in chain:
                chain.append(candidate)
        return chain

    @property
    def rotator(self) -> GeminiAPIRotator:
        """Underlying GeminiAPIRotator instance."""
        return self._rotator

    def close(self) -> None:
        """Close database adapter connection if present.

        Pending audit rows are flushed first (bounded): the adapter owns a single
        connection, so closing it under the audit thread would lose those rows.
        """
        pending = flush_gemini_audit(5.0)
        if pending:
            logger.warning(
                "%d gemini_usage row(s) were still queued when the provider closed.", pending
            )
        if self._db_adapter and hasattr(self._db_adapter, "close"):
            self._db_adapter.close()

    @property
    def primary_disabled(self) -> bool:
        """Whether the primary model has been skipped for the rest of this process."""
        return self._primary_disabled

    async def _execute_model(
        self,
        model: str,
        prompt: str,
        system_instruction: str | None,
        extra_config: dict[str, Any],
        timeout: float | None = None,
        *,
        article_id: int | None = None,
        source_id: int | None = None,
        operation: str = "analysis",
    ) -> Any:
        """Run one model call under a hard timeout.

        The google-genai SDK retries 5xx internally with its own backoff, which in
        production produced single calls that blocked for 700+ seconds. Two ceilings
        stop that: ``http_options["timeout"]`` (built in ``generate_json``) makes the
        SDK's own HTTP request abort at the same ceiling in milliseconds, and ``wait_for``
        bounds the await.

        Cancellation boundary (do not "fix" this by assuming otherwise): the rotator
        runs the blocking SDK call in ``loop.run_in_executor`` (gemini_rotator
        middleware), so cancelling the awaited future only detaches the awaiter. With the
        SDK-level timeout the request now dies at the ceiling instead of keeping its
        worker thread busy for the SDK's whole retry cycle - that leftover thread is what
        starved the shared default executor and produced the 171-second gaps between
        consecutive articles in run #237.
        """
        start_time = time.monotonic()
        try:
            response = await asyncio.wait_for(
                self._rotator.execute(
                    prompt,
                    model=model,
                    system=system_instruction,
                    extra_config=extra_config,
                ),
                timeout=timeout if timeout is not None else self._request_timeout,
            )
        except Exception as exc:  # noqa: BLE001 - recorded and re-raised
            latency_ms = (time.monotonic() - start_time) * 1000.0
            status = "timeout" if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) else "error"
            await self._record_gemini_usage(
                article_id=article_id,
                source_id=source_id,
                model_name=model,
                usage=None,
                operation=operation,
                status=status,
                latency_ms=latency_ms,
                error_message=str(exc),
            )
            raise

        latency_ms = (time.monotonic() - start_time) * 1000.0
        usage = self._extract_usage_metadata(response)
        has_usage = any(
            value is not None
            for value in (
                usage.get("prompt_token_count"),
                usage.get("candidates_token_count"),
                usage.get("total_token_count"),
                usage.get("thoughts_token_count"),
                usage.get("cached_content_token_count"),
            )
        )
        await self._record_gemini_usage(
            article_id=article_id,
            source_id=source_id,
            model_name=model,
            usage=usage,
            operation=operation,
            status="success" if has_usage else "no_usage",
            latency_ms=latency_ms,
            error_message=None,
        )
        return response

    def _timeout_for(self, purpose: str) -> float:
        """Per-purpose ceiling; a perspective matrix is slower than one article."""
        if purpose == "perspectives":
            return max(self._request_timeout, float(settings.ai_perspectives_timeout_seconds))
        return self._request_timeout

    async def _execute_model_with_timeout_retry(
        self,
        model: str,
        prompt: str,
        system_instruction: str | None,
        extra_config: dict[str, Any],
        *,
        timeout: float,
        attempts: int,
        purpose: str,
        article_id: int | None = None,
        source_id: int | None = None,
    ) -> Any:
        """Run one call, retrying at most once and only after a timeout.

        Every timeout is logged, and the final one is re-raised, so the caller's normal
        failure path (fallback model, then ``pipeline``'s ``logger.exception``) still sees
        it; nothing is swallowed or turned into a silent empty result.

        Known limitation: a timed-out attempt is *not* guaranteed to have stopped. Because
        the SDK call runs in a thread (see ``_execute_model``), the retry can overlap with
        the previous in-flight request, which keeps consuming quota and a per-key slot
        until it finishes. Preventing that would require the rotator to expose a
        cancellable/async transport; until then the overlap is accepted deliberately and
        bounded (one retry, one fallback call). Operators can set
        ``AI_PERSPECTIVES_RETRIES=0`` to opt out of the retry entirely.
        """
        for attempt in range(1, attempts + 1):
            try:
                return await self._execute_model(
                    model,
                    prompt,
                    system_instruction,
                    extra_config,
                    timeout=timeout,
                    article_id=article_id,
                    source_id=source_id,
                    operation=purpose,
                )
            except TimeoutError:
                if attempt >= attempts:
                    raise
                logger.warning(
                    "Gemini [%s] call timed out after %.0fs (attempt %d/%d); retrying.",
                    purpose,
                    timeout,
                    attempt,
                    attempts,
                )
                await asyncio.sleep(2.0 * attempt)
        raise AssertionError("unreachable: the retry loop always returns or raises")

    def _record_primary_failure(self) -> None:
        """Trip the model-level circuit breaker after repeated primary failures."""
        self._primary_failures += 1
        if (
            not self._primary_disabled
            and self._enable_fallback
            and self._fallback_model
            and self._primary_failures >= self.PRIMARY_FAILURE_THRESHOLD
        ):
            self._primary_disabled = True
            logger.warning(
                "Model circuit breaker open: %s failed %d times in a row; "
                "using %s for the rest of this run.",
                self._primary_model,
                self._primary_failures,
                self._fallback_model,
            )

    @staticmethod
    def _extract_usage_metadata(response: Any) -> dict[str, int | None]:
        """Extract usage metadata from a google.genai response without guessing missing values."""
        usage = getattr(response, "usage_metadata", None)
        if usage is None:
            return {
                "prompt_token_count": None,
                "candidates_token_count": None,
                "total_token_count": None,
                "thoughts_token_count": None,
                "cached_content_token_count": None,
            }

        def _get(name: str) -> int | None:
            value = getattr(usage, name, None)
            if value is None and isinstance(usage, dict):
                value = usage.get(name)
            return value if isinstance(value, int) else None

        return {
            "prompt_token_count": _get("prompt_token_count"),
            "candidates_token_count": _get("candidates_token_count"),
            "total_token_count": _get("total_token_count"),
            "thoughts_token_count": _get("thoughts_token_count"),
            "cached_content_token_count": _get("cached_content_token_count"),
        }

    async def _record_gemini_usage(
        self,
        *,
        article_id: int | None,
        source_id: int | None,
        model_name: str,
        usage: dict[str, int | None] | None,
        operation: str,
        status: str,
        latency_ms: float,
        error_message: str | None,
    ) -> None:
        """Queue one attempt for gemini_usage without delaying the model response.

        The insert runs on the dedicated audit thread (see ``_audit_worker``), never on
        the default executor that carries the blocking SDK calls: awaiting it there put a
        slow or queued audit write in front of every successful model answer.
        """
        adapter = getattr(self, "_db_adapter", None)
        if adapter is None or not hasattr(adapter, "record_gemini_usage"):
            return
        data = usage or {}
        try:
            _ensure_audit_worker()
            _AUDIT_QUEUE.put(
                (
                    adapter.record_gemini_usage,
                    {
                        "article_id": article_id,
                        "source_id": source_id,
                        "model_name": model_name,
                        "prompt_token_count": data.get("prompt_token_count"),
                        "candidates_token_count": data.get("candidates_token_count"),
                        "total_token_count": data.get("total_token_count"),
                        "thoughts_token_count": data.get("thoughts_token_count"),
                        "cached_content_token_count": data.get("cached_content_token_count"),
                        "operation": operation,
                        "status": status,
                        "latency_ms": latency_ms,
                        "error_message": error_message,
                    },
                )
            )
        except Exception:  # noqa: BLE001 - audit must not break AI pipeline
            logger.exception("Failed to queue the gemini_usage row; continuing.")

    async def generate_json(
        self,
        prompt: str,
        system_instruction: str | None = None,
        *,
        purpose: Literal["analysis", "preflight", "schema_repair", "perspectives"] = "analysis",
        article_id: int | None = None,
        source_id: int | None = None,
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
        timeout = self._timeout_for(purpose)
        # HttpOptions.timeout is the SDK's per-request ceiling in MILLISECONDS
        # (google.genai.types.HttpOptions: "Timeout for the request in milliseconds").
        # Without it the request outlives ``wait_for`` on its executor thread until the
        # SDK's own retries finish; with it the client closes the connection itself.
        extra_config: dict[str, Any] = {
            "response_mime_type": "application/json",
            "temperature": 0.1,
            "http_options": {
                "api_version": GEMINI_API_VERSION,
                "timeout": int(timeout * 1000),
                # google-genai retries retryable status codes five times by default
                # and applies HttpOptions.timeout to every attempt, so one call
                # could stack ~5 x 45s plus backoff (the silent 264-second gaps in
                # run #244). One attempt, then the model chain takes over.
                "retry_options": {"attempts": 1},
            },
        }

        # Ordered chain: primary, then the Lite fallback, then the last-resort tertiary
        # model. A 503 (high demand) on both Lite models continues to the tertiary model
        # instead of failing the article and tripping the pipeline's circuit breaker.
        chain = self._model_chain()
        if self._primary_disabled and len(chain) > 1:
            chain = chain[1:]

        start_time = time.monotonic()
        raw_text: str | None = None

        # Clamped again at the call site: a directly mutated setting must never turn
        # into an unbounded retry loop.
        perspective_retries = min(max(int(settings.ai_perspectives_retries), 0), 1)
        attempts = 1 + (perspective_retries if purpose == "perspectives" else 0)

        for index, candidate_model in enumerate(chain):
            try:
                if index == 0:
                    response = await self._execute_model_with_timeout_retry(
                        candidate_model,
                        prompt,
                        system_instruction,
                        extra_config,
                        timeout=timeout,
                        attempts=attempts,
                        purpose=purpose,
                        article_id=article_id,
                        source_id=source_id,
                    )
                else:
                    response = await self._execute_model(
                        candidate_model,
                        prompt,
                        system_instruction,
                        extra_config,
                        timeout=timeout,
                        article_id=article_id,
                        source_id=source_id,
                        operation=purpose,
                    )
            except Exception as exc:
                if candidate_model == self._primary_model:
                    self._record_primary_failure()
                # Only a transient/quota error justifies spending quota on the next model;
                # a malformed request or bad key would fail there identically.
                if is_retryable_ai_error(exc) and index < len(chain) - 1:
                    logger.warning(
                        "Model %s encountered retryable error (%s). Attempting fallback to %s.",
                        candidate_model,
                        exc,
                        chain[index + 1],
                    )
                    continue
                self._last_used_model = self._primary_model
                self._model = self._primary_model
                _translate_and_raise(exc)

            raw_text = getattr(response, "text", "") or ""
            self._last_used_model = candidate_model
            self._model = candidate_model
            latency = time.monotonic() - start_time
            # Kept in the log so an operator can tell a primary call from a fallback one.
            fallback_prefix = "Fallback " if index > 0 else ""

            if purpose == "preflight":
                GeminiProvider._preflight_calls += 1
                logger.info(
                    "%sGemini PRE-FLIGHT check #%d completed: model=%s, latency=%.2fs",
                    fallback_prefix,
                    GeminiProvider._preflight_calls,
                    candidate_model,
                    latency,
                )
            else:
                GeminiProvider._total_calls += 1
                self.call_count += 1
                logger.info(
                    "%sGemini API call #%d completed [%s]: model=%s, prompt_len=%d, latency=%.2fs",
                    fallback_prefix,
                    GeminiProvider._total_calls,
                    purpose,
                    candidate_model,
                    len(prompt),
                    latency,
                )
            break

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
            raise AIProviderError(
                f"Invalid JSON returned by Gemini: {exc}", raw_length=len(raw_text)
            ) from exc


DEFAULT_VALID_CANNED: dict[str, Any] = {
    "is_relevant": True,
    "is_injection_attempt": False,
    "categories": ["POL"],
    "event_title": "Mock Event",
    "headline_fa": "تیتر موک",
    "summary_fa": "خلاصه موک",
    "countries": ["Iran"],
    "actors": [],
    "location": None,
    "importance": 3,
    "verification_status": "unconfirmed",
}


class MockAIProvider(BaseAIProvider):
    """Deterministic mock provider for unit testing and offline development."""

    def __init__(self, canned_response: dict[str, Any] | None = None, model: str = "mock-model-v1"):
        self._canned_response = canned_response if canned_response is not None else dict(DEFAULT_VALID_CANNED)
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
        purpose: Literal["analysis", "preflight", "schema_repair", "perspectives"] = "analysis",
        article_id: int | None = None,
        source_id: int | None = None,
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
