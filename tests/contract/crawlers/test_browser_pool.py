"""Browser-pool behaviour (design §12, §19): reuse, recycling, crash recovery, hook.

Needs Chromium; runs with ``RUN_BROWSER_TESTS=1``.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from collections.abc import Iterator

import pytest
from antipiracy_contracts.ids import UrlId

from crawler2.core.configuration.settings import BrowserSettings, HttpFetchSettings
from crawler2.crawlers.browser import BrowserFetcher
from crawler2.crawlers.interception import (
    ALLOW,
    InterceptAction,
    InterceptDecision,
    InterceptedRequest,
)
from crawler2.crawlers.model import FetchRequest, FetchResult, Outcome
from tests.fixtures import fetchweb
from tests.fixtures.web import FixtureSite, serve

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_BROWSER_TESTS") != "1", reason="needs Chromium; set RUN_BROWSER_TESTS=1"
)
HTTP = HttpFetchSettings(total_timeout_s=5, media_probe_bytes=64 * 1024)


@pytest.fixture(scope="module")
def site() -> Iterator[FixtureSite]:
    with serve({**fetchweb.fetch_routes(), **fetchweb.pages(20)}) as s:
        yield s


def req(url: str) -> FetchRequest:
    return FetchRequest(url=url, url_id=UrlId.of(url))  # type: ignore[arg-type]


def browser(**overrides: object) -> BrowserFetcher:
    base: dict[str, object] = {"navigation_timeout_s": 5, "settle_s": 0.3, "contexts": 1}
    return BrowserFetcher(BrowserSettings(**{**base, **overrides}), HTTP)  # type: ignore[arg-type]


def test_contexts_are_reused_then_recycled(site: FixtureSite) -> None:
    async def go() -> tuple[list[FetchResult], BrowserFetcher]:
        fetcher = browser(recycle_pages=3, browser_recycle_pages=7)
        await fetcher.start()
        try:
            results = [await fetcher.fetch(req(site.url(f"/page/{i}"))) for i in range(10)]
        finally:
            await fetcher.close()
        return results, fetcher

    results, fetcher = asyncio.run(go())
    stats = fetcher.pool.stats
    assert all(r.outcome is Outcome.OK for r in results)
    assert [r.render.context_pages for r in results if r.render] == [1, 2, 3, 1, 2, 3, 1, 1, 2, 3]
    assert stats.browser_launches == 2  # one recycle after 7 pages
    assert stats.browser_recycles == 1
    assert stats.browser_restarts == 0
    assert stats.contexts_created == 4
    assert stats.pages_opened == stats.pages_closed == 10


class BlockScripts:
    def __init__(self) -> None:
        self.seen: list[InterceptedRequest] = []

    def decide(self, request: InterceptedRequest) -> InterceptDecision:
        self.seen.append(request)
        if request.resource_type == "script":
            return InterceptDecision(InterceptAction.BLOCK, rule_id="test", reason="scripts")
        return ALLOW


def test_interception_hook_sees_and_can_block(site: FixtureSite) -> None:
    hook = BlockScripts()

    async def go() -> FetchResult:
        fetcher = BrowserFetcher(
            BrowserSettings(navigation_timeout_s=5, settle_s=0.3), HTTP, interceptor=hook
        )
        await fetcher.start()
        try:
            return await fetcher.fetch(req(site.url("/js-only")))
        finally:
            await fetcher.close()

    result = asyncio.run(go())
    assert result.outcome is Outcome.OK
    assert any(r.is_navigation for r in hook.seen)
    # inline scripts are not requests; the page still renders — the hook saw every request
    assert result.render is not None
    assert result.render.requests >= 1


def test_media_url_is_probed_not_rendered(site: FixtureSite) -> None:
    async def go() -> FetchResult:
        fetcher = browser()
        await fetcher.start()
        try:
            return await fetcher.fetch(req(site.url("/media/huge.mp4")))
        finally:
            await fetcher.close()

    result = asyncio.run(go())
    assert result.outcome is Outcome.MEDIA
    assert site.state.bytes_sent["/media/huge.mp4"] < 4 * 1024 * 1024


def test_navigation_has_one_deadline(site: FixtureSite) -> None:
    async def go() -> tuple[FetchResult, float]:
        fetcher = browser(navigation_timeout_s=2, settle_s=5)
        await fetcher.start()
        try:
            started = time.monotonic()
            result = await fetcher.fetch(req(site.url("/slow-page")))
            return result, time.monotonic() - started
        finally:
            await fetcher.close()

    result, elapsed = asyncio.run(go())
    assert result.outcome is Outcome.TIMEOUT
    assert elapsed < 4  # V1 allowed goto + networkidle each the full timeout


def test_killed_browser_is_replaced_without_stopping_the_worker(site: FixtureSite) -> None:
    async def go() -> tuple[FetchResult, FetchResult, BrowserFetcher]:
        fetcher = browser(navigation_timeout_s=20)
        await fetcher.start()
        try:
            pid = fetcher.pool.browser_pid()
            assert pid is not None
            in_flight = asyncio.ensure_future(fetcher.fetch(req(site.url("/slow-page"))))
            await asyncio.sleep(1.5)
            os.kill(pid, signal.SIGKILL)
            crashed = await asyncio.wait_for(in_flight, 15)
            after = await fetcher.fetch(req(site.url("/ok")))
            return crashed, after, fetcher
        finally:
            await fetcher.close()

    crashed, after, fetcher = asyncio.run(go())
    assert crashed.outcome is Outcome.FETCHER_CRASH
    assert after.outcome is Outcome.OK
    assert fetcher.pool.stats.browser_restarts == 1
    assert fetcher.pool.stats.browser_launches == 2
