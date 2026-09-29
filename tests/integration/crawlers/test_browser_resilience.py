"""Browser chaos through the frontier (design §26) and the 1 000-page leak test (§25)."""

from __future__ import annotations

import asyncio
import json
import os
import signal

import pytest
from antipiracy_contracts.ids import UrlId
from antipiracy_contracts.urls import canonicalize_url

from crawler2.core.configuration import ExecutionQueue
from crawler2.core.configuration.settings import BrowserSettings
from crawler2.crawlers.browser import BrowserFetcher
from crawler2.crawlers.model import FetchRequest, Outcome
from tests.fixtures.web import FixtureSite
from tests.integration.crawlers.conftest import (
    HTTP_SETTINGS,
    FrontierFactory,
    MemoryRecorder,
    admit,
    browser_only,
    runtime,
)

pytestmark = [pytest.mark.integration, browser_only]
BROWSER = ExecutionQueue.BROWSER


def test_killing_the_browser_mid_page_loses_no_task(
    site: FixtureSite, make_frontier: FrontierFactory
) -> None:
    frontier = make_frontier(max_attempts=2)
    admit(frontier, site.url("/slow-page"), BROWSER)
    fetcher = BrowserFetcher(
        BrowserSettings(navigation_timeout_s=3, settle_s=0.2, contexts=2), HTTP_SETTINGS
    )
    recorder = MemoryRecorder()
    rt = runtime(frontier, BROWSER, fetcher, recorder, concurrency=2)

    async def go() -> None:
        task = asyncio.ensure_future(rt.run())
        while fetcher.pool.stats.pages_opened == 0:  # the slow page is in flight
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.5)
        pid = fetcher.pool.browser_pid()
        assert pid is not None
        os.kill(pid, signal.SIGKILL)
        # later work (admitted once the crash was detected and reported) must
        # still be processed by the same worker process
        while not recorder.outcomes_for(site.url("/slow-page")):
            await asyncio.sleep(0.05)
        for i in range(5):
            admit(frontier, site.url(f"/page/{i}"), BROWSER)
        for _ in range(300):
            await asyncio.sleep(0.1)
            if (await asyncio.to_thread(frontier.stats)).depth[BROWSER] == 0:
                break
        rt.stop()
        await asyncio.wait_for(task, 30)

    asyncio.run(go())
    slow = recorder.outcomes_for(site.url("/slow-page"))
    assert slow[0] == Outcome.FETCHER_CRASH.value  # detected and reported, not swallowed
    assert slow[1:] == [Outcome.TIMEOUT.value]  # retried by the frontier, then exhausted
    assert rt.stats.reports["fail:retry_scheduled"] >= 1
    assert rt.stats.reports["fail:exhausted"] == 1
    pages = [recorder.outcomes_for(site.url(f"/page/{i}")) for i in range(5)]
    assert all(p == ["ok"] for p in pages), pages
    assert fetcher.pool.stats.browser_restarts == 1
    assert frontier.stats().depth[BROWSER] == 0  # nothing lost, nothing stuck


def test_thousand_pages_keep_the_browser_bounded(site: FixtureSite) -> None:
    settings = BrowserSettings(
        contexts=2,
        recycle_pages=50,
        browser_recycle_pages=500,
        navigation_timeout_s=10,
        settle_s=0.0,
    )
    fetcher = BrowserFetcher(settings, HTTP_SETTINGS)
    pages = 1000

    async def go() -> list[Outcome]:
        await fetcher.start()
        queue: asyncio.Queue[int] = asyncio.Queue()
        for i in range(pages):
            queue.put_nowait(i)
        outcomes: list[Outcome] = []

        async def slot() -> None:
            while not queue.empty():
                i = queue.get_nowait()
                url = site.url(f"/page/{i}")
                result = await fetcher.fetch(FetchRequest(url, url_id=_uid(url)))
                outcomes.append(result.outcome)

        try:
            await asyncio.gather(slot(), slot())
        finally:
            await fetcher.close()
        return outcomes

    outcomes = asyncio.run(go())
    stats = fetcher.pool.stats
    samples = dict(stats.rss_samples)
    first = [rss for page, rss in samples.items() if 100 <= page <= 500]
    second = [rss for page, rss in samples.items() if page >= 600]
    first_max, second_max, overall = max(first), max(second), max(samples.values())
    report: dict[str, object] = {
        "pages": pages,
        "ok": sum(o is Outcome.OK for o in outcomes),
        "browser_launches": stats.browser_launches,
        "browser_recycles": stats.browser_recycles,
        "browser_restarts": stats.browser_restarts,
        "contexts_created": stats.contexts_created,
        "contexts_closed": stats.contexts_closed,
        "pages_opened": stats.pages_opened,
        "pages_closed": stats.pages_closed,
        "rss_mb_max_pages_100_500": first_max,
        "rss_mb_max_pages_600_1000": second_max,
        "rss_mb_max": overall,
        "rss_mb_by_page": {str(k): v for k, v in samples.items() if k % 50 == 0},
    }
    print("LEAK_TEST " + json.dumps(report))
    assert report["ok"] == pages
    assert stats.pages_opened == stats.pages_closed == pages
    assert stats.browser_recycles >= 1
    assert stats.browser_restarts == 0
    assert stats.contexts_created >= 20
    assert stats.contexts_created - stats.contexts_closed == 0  # everything closed at the end
    assert second_max <= 1.25 * first_max
    assert overall < settings.browser_rss_limit_mb


def _uid(url: str) -> UrlId:
    return UrlId.of(canonicalize_url(url))
