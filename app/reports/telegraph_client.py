"""Async client for the Telegraph API (telegra.ph).

The 12-hour report is published as Telegraph pages: one page per news subject and one
index page that links them. Telegram then only carries a notification with the index
link, so the full report never depends on Telegram's message limits.

The API is a plain JSON-over-HTTP service. Callers pass an access token; when none is
configured the client can create a throwaway account for the run (Telegraph has no way
to recover an account without its token, so operators should persist one).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

MAX_TITLE_CHARS = 256
# Telegraph rejects a page whose serialised content exceeds 64 KB.
MAX_CONTENT_BYTES = 64 * 1024
_CONTENT_TRIM_BUFFER = 512
_SHORT_NAME_RE = re.compile(r"[^A-Za-z0-9_]")


class TelegraphError(RuntimeError):
    """Any failure talking to the Telegraph API."""


class TelegraphNotConfiguredError(TelegraphError):
    """Telegraph is disabled or no author/account information is available."""


@dataclass(frozen=True)
class TelegraphPage:
    """A created or edited Telegraph page."""

    path: str
    url: str
    title: str


def _truncate(value: str, limit: int) -> str:
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def author_short_name(name: str | None = None) -> str:
    """Derive a Telegraph-legal short name (1-32 chars, ``[A-Za-z0-9_]``)."""
    raw = (name or settings.telegraph_short_name or "").strip()
    cleaned = _SHORT_NAME_RE.sub("_", raw)[:32].strip("_")
    return cleaned or "middle_east_news"


def build_content(nodes: list[dict[str, Any]]) -> str:
    """Serialise node content, dropping trailing blocks until it fits Telegraph's cap.

    A single oversized page would otherwise fail the whole subject, so the content is
    trimmed (never the title or the source link, which come first) instead.
    """
    trimmed = [node for node in nodes if node]
    while trimmed:
        serialised = json.dumps(trimmed, ensure_ascii=False)
        if len(serialised.encode("utf-8")) <= MAX_CONTENT_BYTES - _CONTENT_TRIM_BUFFER:
            return serialised
        trimmed.pop()
    return "[]"


class TelegraphClient:
    """Thin async wrapper over ``https://api.telegra.ph``."""

    def __init__(
        self,
        access_token: str | None = None,
        author_name: str | None = None,
        author_url: str | None = None,
        api_base: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.access_token = (access_token if access_token is not None else settings.telegraph_access_token) or ""
        self.author_name = author_name or settings.telegraph_author_name or ""
        self.author_url = author_url if author_url is not None else settings.telegraph_author_url or ""
        self.api_base = (api_base or settings.telegraph_api_base or "https://api.telegra.ph").rstrip("/")
        self.timeout = float(timeout if timeout is not None else settings.telegraph_timeout_seconds)

    async def _call(self, method: str, **params: Any) -> Any:
        payload = {key: value for key, value in params.items() if value is not None}
        url = f"{self.api_base}/{method}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPError as exc:
            raise TelegraphError(f"Telegraph {method} request failed: {exc}") from exc
        except ValueError as exc:
            raise TelegraphError(f"Telegraph {method} returned a non-JSON body") from exc

        if not isinstance(body, dict) or not body.get("ok"):
            error = body.get("error") if isinstance(body, dict) else body
            raise TelegraphError(f"Telegraph {method} rejected the request: {error}")
        return body.get("result")

    async def create_account(self, short_name: str | None = None) -> str:
        """Create a Telegraph account and remember its token for this client."""
        result = await self._call(
            "createAccount",
            short_name=author_short_name(short_name),
            author_name=self.author_name or None,
            author_url=self.author_url or None,
        )
        token = (result or {}).get("access_token") if isinstance(result, dict) else None
        if not token:
            raise TelegraphError("Telegraph createAccount returned no access_token")
        self.access_token = str(token)
        logger.warning(
            "Created a temporary Telegraph account. Persist TELEGRAPH_ACCESS_TOKEN=%s to stop "
            "creating a new account on every run; pages made by a throwaway account cannot be "
            "edited later.",
            self.access_token,
        )
        return self.access_token

    async def ensure_token(self) -> str:
        if self.access_token:
            return self.access_token
        return await self.create_account()

    async def create_page(
        self,
        title: str,
        content: list[dict[str, Any]],
        author_name: str | None = None,
        author_url: str | None = None,
    ) -> TelegraphPage:
        token = await self.ensure_token()
        result = await self._call(
            "createPage",
            access_token=token,
            title=_truncate(title, MAX_TITLE_CHARS),
            author_name=author_name or self.author_name or None,
            author_url=author_url or self.author_url or None,
            content=build_content(content),
            return_content=False,
        )
        return _page_from_result(result, title)

    async def edit_page(
        self,
        path: str,
        title: str,
        content: list[dict[str, Any]],
        author_name: str | None = None,
    ) -> TelegraphPage:
        token = await self.ensure_token()
        result = await self._call(
            "editPage",
            access_token=token,
            path=path,
            title=_truncate(title, MAX_TITLE_CHARS),
            author_name=author_name or self.author_name or None,
            content=build_content(content),
            return_content=False,
        )
        page = _page_from_result(result, title)
        # editPage may omit the path/url when only the title changed; keep the caller's path.
        if not page.path:
            return TelegraphPage(path=path, url=page.url, title=page.title)
        return page

    async def get_page(self, path: str, return_content: bool = False) -> dict[str, Any]:
        result = await self._call(
            "getPage",
            path=path,
            return_content="true" if return_content else None,
        )
        return result if isinstance(result, dict) else {}


def _page_from_result(result: Any, fallback_title: str) -> TelegraphPage:
    if not isinstance(result, dict):
        raise TelegraphError("Telegraph returned an unexpected page payload")
    path = str(result.get("path") or "")
    url = str(result.get("url") or (f"https://telegra.ph/{path}" if path else ""))
    return TelegraphPage(path=path, url=url, title=str(result.get("title") or fallback_title))


def build_client() -> TelegraphClient | None:
    """Return a client when Telegraph publishing is enabled, else ``None``."""
    if not settings.telegraph_enabled:
        logger.info("Telegraph publishing is disabled (TELEGRAPH_ENABLED=false).")
        return None
    return TelegraphClient()
