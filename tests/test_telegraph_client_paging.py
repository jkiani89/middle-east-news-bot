"""getPageList paging: a recovery must see the whole account, not one window.

``getPageList`` returns a window per request, so concluding that a page does not exist
from a single call is exactly how a crashed create becomes a duplicate page. A window
list alone is not proof either: a page created or deleted while the walk runs shifts the
offsets and can drop a page while the collected count still matches ``total_count``.

The walk is therefore pinned to the ``total_count`` of its first response, requires the
same count from every window and from a read after the walk, rejects repeated paths, and
is repeated in full. Only two agreeing, individually proven walks make a listing complete.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from app.reports.telegraph_client import (
    MAX_PAGE_LIST_REQUESTS,
    PAGE_LIST_PAGE_SIZE,
    TelegraphClient,
    TelegraphError,
)


class _PagedClient(TelegraphClient):
    """Client whose ``list_page_window`` serves a synthetic account tests can mutate."""

    def __init__(
        self,
        total: int = 0,
        *,
        reported_total: int | None = None,
        reports_total: bool = True,
        fail_at: int | None = None,
        always_full: bool = False,
    ) -> None:
        super().__init__(access_token="token")
        self.account: list[dict] = [{"path": f"p{index}"} for index in range(total)]
        self.reported_total = reported_total
        self.reports_total = reports_total
        self.fail_at = fail_at
        self.always_full = always_full
        self.offsets: list[int] = []
        self.requests = 0
        self.walks = 0
        # Request index (1-based) -> hook run just before that window is served.
        self.before_request: dict[int, Callable[[_PagedClient], None]] = {}
        # Request indexes that fail with a network error.
        self.fail_on_request: set[int] = set()
        # What the post-walk ``limit=1`` confirmation reports, when it must differ.
        self.confirm_total: int | None = None
        # Full override: (client, walk, offset, limit) -> (pages, total).
        self.script: Callable[[_PagedClient, int, int, int], tuple[list[dict], int | None]] | None = None

    async def list_page_window(
        self, offset: int = 0, limit: int = PAGE_LIST_PAGE_SIZE
    ) -> tuple[list[dict], int | None]:
        self.requests += 1
        self.offsets.append(offset)
        if limit == PAGE_LIST_PAGE_SIZE and offset == 0:
            self.walks += 1
        if self.requests in self.fail_on_request:
            raise TelegraphError("getPageList request failed")
        if self.fail_at is not None and offset >= self.fail_at:
            raise TelegraphError("listing exploded")
        hook = self.before_request.get(self.requests)
        if hook is not None:
            hook(self)
        if self.script is not None:
            return self.script(self, self.walks, offset, limit)
        if self.always_full:
            return (
                [{"path": f"full{offset + index}"} for index in range(limit)],
                self.reported_total,
            )
        batch = list(self.account[offset : offset + limit])
        if not self.reports_total:
            return batch, None
        if limit < PAGE_LIST_PAGE_SIZE and self.confirm_total is not None:
            return batch, self.confirm_total
        return batch, self.reported_total if self.reported_total is not None else len(self.account)


def _window(client: _PagedClient, offset: int, limit: int, *, total: int | None = None):
    """One honest window of the client's account, optionally with a fake total_count."""
    return (
        list(client.account[offset : offset + limit]),
        len(client.account) if total is None else total,
    )


@pytest.mark.asyncio
async def test_the_walk_reaches_pages_beyond_the_first_window():
    client = _PagedClient(450)

    pages, complete = await client.list_pages_until_end()

    assert complete is True
    assert len(pages) == 450
    # Two full walks, each followed by one confirmation read at offset 0.
    assert client.walks == 2
    assert client.offsets == [0, 200, 400, 0, 0, 200, 400, 0]
    assert pages[-1]["path"] == "p449"


@pytest.mark.asyncio
async def test_an_exact_multiple_of_the_window_is_still_complete():
    """A full last window must not look like the end of the list."""
    client = _PagedClient(2 * PAGE_LIST_PAGE_SIZE)

    pages, complete = await client.list_pages_until_end()

    assert complete is True
    assert len(pages) == 2 * PAGE_LIST_PAGE_SIZE
    assert client.walks == 2


@pytest.mark.asyncio
async def test_an_empty_account_is_a_complete_listing():
    client = _PagedClient(0)

    pages, complete = await client.list_pages_until_end()

    assert pages == []
    assert complete is True
    assert client.walks == 2


@pytest.mark.asyncio
async def test_fewer_pages_than_total_count_is_unproven():
    """The account says it holds more pages than the walk delivered."""
    client = _PagedClient(3, reported_total=5)

    pages, complete = await client.list_pages_until_end()

    assert len(pages) == 3
    assert complete is False


@pytest.mark.asyncio
async def test_more_pages_than_total_count_is_unproven():
    """A page count that contradicts total_count proves nothing either way."""
    client = _PagedClient(3, reported_total=2)

    pages, complete = await client.list_pages_until_end()

    assert len(pages) == 3
    assert complete is False


@pytest.mark.asyncio
async def test_a_missing_total_count_is_unproven():
    """No count means no proof: nothing collected is presented as the account's pages."""
    client = _PagedClient(3, reports_total=False)

    pages, complete = await client.list_pages_until_end()

    assert pages == []
    assert complete is False
    assert client.walks == 1


@pytest.mark.asyncio
async def test_an_error_midway_propagates_instead_of_looking_empty():
    client = _PagedClient(450, fail_at=PAGE_LIST_PAGE_SIZE)

    with pytest.raises(TelegraphError):
        await client.list_pages_until_end()


@pytest.mark.asyncio
async def test_the_safety_valve_reports_an_incomplete_listing():
    """Never claim the list ended: the caller must refuse to create a page."""
    client = _PagedClient(0, reported_total=10**6, always_full=True)

    pages, complete = await client.list_pages_until_end()

    assert complete is False
    assert len(pages) == MAX_PAGE_LIST_REQUESTS * PAGE_LIST_PAGE_SIZE
    assert len(client.offsets) == MAX_PAGE_LIST_REQUESTS


@pytest.mark.asyncio
async def test_a_total_count_that_changes_between_windows_is_unproven():
    """The first response pins the count; every later window must repeat it."""
    client = _PagedClient(450)
    client.script = lambda c, walk, offset, limit: _window(
        c, offset, limit, total=451 if offset >= 2 * PAGE_LIST_PAGE_SIZE else None
    )

    pages, complete = await client.list_pages_until_end()

    assert complete is False
    assert client.walks == 1  # the contradiction stops the walk immediately


@pytest.mark.asyncio
async def test_a_page_deleted_during_the_walk_is_unproven():
    """Re-reading the count would make 399 collected pages look like a proof; the pin stops it.

    This is the case that used to pass: the deck shrank while the walk ran, so the second
    window shifted and could skip ``p200``, and the fresh ``total_count`` (399) matched the
    shortened result. With the count pinned to the first response the contradiction is
    caught on the spot.
    """
    client = _PagedClient(400)
    client.before_request[2] = lambda c: c.account.pop(0)

    pages, complete = await client.list_pages_until_end()

    assert complete is False
    assert client.walks == 1
    assert len(pages) == 200  # nothing of the shifted window is trusted
    assert client.offsets == [0, PAGE_LIST_PAGE_SIZE]


@pytest.mark.asyncio
async def test_a_page_created_during_the_walk_is_unproven():
    """An insert shifts every later window; the changed count exposes it immediately."""
    client = _PagedClient(400)
    client.before_request[2] = lambda c: c.account.insert(0, {"path": "p-new"})

    pages, complete = await client.list_pages_until_end()

    assert complete is False
    assert client.walks == 1
    assert len(pages) == 200


@pytest.mark.asyncio
async def test_a_repeated_path_is_unproven_even_when_the_count_matches():
    """A duplicate path is the visible symptom of shifted offsets, not a page to count."""

    def script(c, walk, offset, limit):
        if limit != PAGE_LIST_PAGE_SIZE:
            return [], len(c.account)
        window = [dict(page) for page in c.account[offset : offset + limit]]
        if offset == 0:
            window[-1] = dict(window[-2])  # p199 vanishes, p198 appears twice
        return window, len(c.account)

    client = _PagedClient(200)
    client.script = script

    pages, complete = await client.list_pages_until_end()

    paths = [page["path"] for page in pages]
    assert complete is False
    assert len(paths) == 200
    assert len(paths) != len(set(paths))


@pytest.mark.asyncio
async def test_two_walks_that_return_different_pages_are_unproven():
    """Each walk may be internally proven; only identical page sets are trusted."""

    def script(c, walk, offset, limit):
        window = [dict(page) for page in c.account[offset : offset + limit]]
        if walk == 2:
            window = [
                {"path": "p-alternate"} if page["path"] == "p399" else page for page in window
            ]
        return window, len(c.account)

    client = _PagedClient(400)
    client.script = script

    pages, complete = await client.list_pages_until_end()

    assert len(pages) == 400
    assert complete is False
    assert client.walks == 2


@pytest.mark.asyncio
async def test_a_second_walk_that_cannot_prove_itself_is_unproven():
    """A proven first walk is not reused when the confirming walk is unproven."""

    def script(c, walk, offset, limit):
        window = [dict(page) for page in c.account[offset : offset + limit]]
        if walk == 2 and limit == PAGE_LIST_PAGE_SIZE and offset == 0:
            window[-1] = dict(window[-2])  # shifted offsets in the second walk only
        return window, len(c.account)

    client = _PagedClient(400)
    client.script = script

    pages, complete = await client.list_pages_until_end()

    assert len(pages) == 400
    assert complete is False
    assert client.walks == 2


@pytest.mark.asyncio
async def test_two_proven_walks_with_different_total_counts_are_unproven():
    """Both walks can prove themselves and still describe two different accounts."""

    def script(c, walk, offset, limit):
        if walk == 2:
            pages = [{"path": "p-extra"}, *c.account]
            return pages[offset : offset + limit], len(pages)
        return _window(c, offset, limit)

    client = _PagedClient(400)
    client.script = script

    pages, complete = await client.list_pages_until_end()

    assert len(pages) == 400
    assert complete is False
    assert client.walks == 2


@pytest.mark.asyncio
async def test_a_total_count_change_at_the_final_control_is_unproven():
    """The post-walk read catches an account that changed while the walk ran."""
    client = _PagedClient(400)
    client.confirm_total = 401

    pages, complete = await client.list_pages_until_end()

    assert len(pages) == 400
    assert complete is False


@pytest.mark.asyncio
async def test_a_network_error_in_the_second_walk_is_unproven():
    """A proven first walk is not reused when the confirming walk fails."""
    client = _PagedClient(400)
    # Requests 1-4 are the first walk (three windows plus its confirmation).
    client.fail_on_request = {5}

    pages, complete = await client.list_pages_until_end()

    assert len(pages) == 400
    assert complete is False
    assert client.walks == 2


@pytest.mark.asyncio
async def test_list_page_window_reads_total_count_from_the_documented_payload(monkeypatch):
    """getPageList returns ``{total_count, pages}``; both are read, nothing is guessed."""
    client = TelegraphClient(access_token="token")

    async def fake_ensure_token() -> str:
        return "token"

    async def fake_call(method: str, **params):
        assert method == "getPageList"
        assert params["offset"] == 200 and params["limit"] == 200
        return {"total_count": 512, "pages": [{"path": "a"}, {"path": "b"}]}

    monkeypatch.setattr(client, "ensure_token", fake_ensure_token)
    monkeypatch.setattr(client, "_call", fake_call)

    pages, total = await client.list_page_window(offset=200, limit=200)

    assert [page["path"] for page in pages] == ["a", "b"]
    assert total == 512


@pytest.mark.asyncio
async def test_a_payload_without_pages_is_an_empty_window(monkeypatch):
    client = TelegraphClient(access_token="token")

    async def fake_ensure_token() -> str:
        return "token"

    async def fake_call(method: str, **params):
        return {"total_count": 0}

    monkeypatch.setattr(client, "ensure_token", fake_ensure_token)
    monkeypatch.setattr(client, "_call", fake_call)

    pages, total = await client.list_page_window()

    assert pages == []
    assert total == 0
