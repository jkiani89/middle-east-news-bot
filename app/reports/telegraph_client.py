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
# getPageList returns one window at a time; recovery must walk all of them.
PAGE_LIST_PAGE_SIZE = 200
# Safety valve for a pathological account (10,000 pages). Reaching it makes the listing
# "incomplete", which forbids creating a page rather than guessing. Each of the two
# confirming walks in ``list_pages_until_end`` may use this many requests.
MAX_PAGE_LIST_REQUESTS = 50
_SHORT_NAME_RE = re.compile(r"[^A-Za-z0-9_]")


class TelegraphError(RuntimeError):
    """Any failure talking to the Telegraph API."""


class TelegraphContentTooLargeError(TelegraphError):
    """A page cannot fit its content inside Telegraph's 64 KB limit.

    Publishing is refused instead of silently dropping trailing blocks: a page that
    loses news is worse than a page that reports it could not be published.
    """

    def __init__(self, size: int, limit: int, *, detail: str = "") -> None:
        self.size = size
        self.limit = limit
        self.detail = detail
        suffix = f" ({detail})" if detail else ""
        super().__init__(
            f"page content is {size} bytes, which exceeds the {limit}-byte Telegraph "
            f"limit{suffix}; split the topic into smaller parts instead of trimming it"
        )


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


def measure_content(nodes: list[dict[str, Any]]) -> int:
    """Size in bytes of the content exactly as it would be sent to Telegraph."""
    return len(json.dumps([node for node in nodes if node], ensure_ascii=False).encode("utf-8"))


def build_content(nodes: list[dict[str, Any]]) -> str:
    """Serialise node content verbatim and refuse an oversized page.

    There is no trimming here on purpose: every news block carries its title, summary,
    source, time, verification label and original link, so dropping the tail of a page
    silently deletes news. Callers split a big subject into several pages instead
    (:mod:`app.reports.telegraph_pagination`) and this function is the last guard that
    turns an unsplittable page into a visible failure.
    """
    kept = [node for node in nodes if node]
    serialised = json.dumps(kept, ensure_ascii=False)
    size = len(serialised.encode("utf-8"))
    if size > MAX_CONTENT_BYTES:
        raise TelegraphContentTooLargeError(size, MAX_CONTENT_BYTES)
    return serialised


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

    async def _walk_page_list(self) -> tuple[list[dict[str, Any]], int | None, bool]:
        """One pass over ``getPageList``, proven against a pinned ``total_count``.

        Returns ``(pages, pinned_total, proven)``. ``total_count`` is pinned from the
        first response and every later window must report exactly that value: an account
        that changed during the walk can shift the offsets, so a window list alone proves
        nothing. A repeated page path is the visible symptom of such a shift. The pass is
        only proven when no path repeated, the collected pages equal the pinned count, and
        a fresh read after the walk still reports the same count.
        """
        pages: list[dict[str, Any]] = []
        seen: set[str] = set()
        total: int | None = None
        repeated_path = False
        walked_to_end = False
        offset = 0
        for _ in range(MAX_PAGE_LIST_REQUESTS):
            batch, reported = await self.list_page_window(
                offset=offset, limit=PAGE_LIST_PAGE_SIZE
            )
            if reported is None:
                logger.warning(
                    "getPageList did not report total_count at offset %s; the listing is unproven.",
                    offset,
                )
                return pages, total, False
            if total is None:
                total = reported
            elif reported != total:
                logger.warning(
                    "getPageList reported total_count %s at offset %s but %s earlier; "
                    "the account changed during the walk and the listing is unproven.",
                    reported,
                    offset,
                    total,
                )
                return pages, total, False
            for page in batch:
                path = str(page.get("path") or "")
                if path and path in seen:
                    repeated_path = True
                seen.add(path)
            pages.extend(batch)
            if len(batch) < PAGE_LIST_PAGE_SIZE:
                walked_to_end = True
                break
            offset += PAGE_LIST_PAGE_SIZE
        if not walked_to_end:
            logger.warning(
                "getPageList still returned full pages after %s requests; the listing is incomplete.",
                MAX_PAGE_LIST_REQUESTS,
            )
            return pages, total, False
        if repeated_path:
            logger.warning(
                "getPageList returned a repeated page path; the window offsets shifted during "
                "the walk and the listing is unproven."
            )
            return pages, total, False
        if len(pages) != total:
            logger.warning(
                "getPageList returned %s pages but total_count is %s; the listing is unproven.",
                len(pages),
                total,
            )
            return pages, total, False
        _confirmation, confirmed_total = await self.list_page_window(offset=0, limit=1)
        if confirmed_total != total:
            logger.warning(
                "getPageList reported total_count %s after the walk but %s during it; "
                "the account changed and the listing is unproven.",
                confirmed_total,
                total,
            )
            return pages, total, False
        return pages, total, True

    async def list_pages_until_end(self) -> tuple[list[dict[str, Any]], bool]:
        """Every page owned by this account, newest first, proven by two walks.

        ``getPageList`` is paged, so a single call can only ever show a window of the
        account's pages: concluding "the page does not exist" from one window is how a
        crashed create turns into a duplicate page. One walk is not enough either, because
        a page created or deleted during the walk shifts the offsets and can drop a page
        while ``total_count`` still matches what was collected. So the account is walked
        twice and the result is trusted only when both walks are internally proven, agree
        on ``total_count`` and return exactly the same set of page paths.

        ``complete`` is False for every unproven case (missing or changing
        ``total_count``, a repeated path, the safety valve, differing walks, a failure of
        the second walk); the caller then refuses to publish on an unproven absence.
        """
        first_pages, first_total, first_proven = await self._walk_page_list()
        if not first_proven:
            return first_pages, False
        try:
            second_pages, second_total, second_proven = await self._walk_page_list()
        except TelegraphError as exc:
            logger.warning(
                "The second getPageList walk failed (%s); the listing is unproven.", exc
            )
            return first_pages, False
        if not second_proven:
            logger.warning(
                "The second getPageList walk could not prove the listing; the first walk is "
                "not trusted on its own."
            )
            return first_pages, False
        if second_total != first_total:
            logger.warning(
                "The two getPageList walks disagree on total_count (%s vs %s); "
                "the listing is unproven.",
                first_total,
                second_total,
            )
            return first_pages, False
        first_paths = {str(page.get("path") or "") for page in first_pages}
        second_paths = {str(page.get("path") or "") for page in second_pages}
        if first_paths != second_paths:
            logger.warning(
                "The two getPageList walks returned different pages; the listing is unproven."
            )
            return first_pages, False
        return first_pages, True

    async def list_page_window(
        self, offset: int = 0, limit: int = 200
    ) -> tuple[list[dict[str, Any]], int | None]:
        """One window of this account's pages, plus the account's reported total count."""
        token = await self.ensure_token()
        result = await self._call("getPageList", access_token=token, offset=offset, limit=limit)
        pages: list[dict[str, Any]] = []
        total: int | None = None
        if isinstance(result, list):
            pages = [page for page in result if isinstance(page, dict)]
        elif isinstance(result, dict):
            batch = result.get("pages")
            if isinstance(batch, list):
                pages = [page for page in batch if isinstance(page, dict)]
            reported = result.get("total_count")
            if isinstance(reported, int) and not isinstance(reported, bool):
                total = reported
        return pages, total

    async def list_pages(self, offset: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        """One window of this account's pages, newest first (used to recover a lost create)."""
        pages, _total = await self.list_page_window(offset=offset, limit=limit)
        return pages


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
