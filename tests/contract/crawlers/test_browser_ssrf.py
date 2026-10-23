"""P14 TC-060: the SSRF guard in a real Chromium (navigation and sub-resources)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator

import pytest
from antipiracy_contracts.models.web import UrlRef

from crawler2.core.configuration.settings import BrowserSettings, HttpFetchSettings
from crawler2.crawlers.browser import BrowserFetcher
from crawler2.crawlers.model import FetchRequest, FetchResult, Outcome
from tests.fixtures.web import Dynamic, FixtureHandler, FixtureSite, Route, serve

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_BROWSER_TESTS") != "1", reason="needs Chromium; set RUN_BROWSER_TESTS=1"
)

SETTINGS = BrowserSettings(
    navigation_timeout_s=10, settle_s=0.5, contexts=1, blocked_resource_types=[]
)
HTML = {"Content-Type": "text/html"}
JS = {"Content-Type": "application/javascript"}


def _page(h: FixtureHandler) -> None:
    h.send_response(200)
    h.send_header("Content-Type", "text/html")
    h.end_headers()
    h.wfile.write(b'<html><body><script src="/static/app.js"></script>hello</body></html>')


@pytest.fixture(scope="module")
def site() -> Iterator[FixtureSite]:
    routes: dict[str, Route | Dynamic] = {
        "/page": Dynamic(_page),
        "/static/app.js": Route(headers=JS, body=b"window.app=1;"),
    }
    with serve(routes) as running:
        yield running


def _fetch(http: HttpFetchSettings, url: str) -> tuple[FetchResult, BrowserFetcher]:
    fetcher = BrowserFetcher(SETTINGS, http)

    async def run() -> FetchResult:
        await fetcher.start()
        try:
            return await fetcher.fetch(FetchRequest(url, UrlRef.of(url).url_id))
        finally:
            await fetcher.close()

    return asyncio.run(run()), fetcher


def test_browser_refuses_navigation_to_loopback_without_exemption(site: FixtureSite) -> None:
    result, _ = _fetch(HttpFetchSettings(), site.url("/page"))
    assert result.outcome is Outcome.UNSAFE_DESTINATION
    assert result.body is None


def test_browser_loads_the_same_page_when_loopback_is_exempt(site: FixtureSite) -> None:
    http = HttpFetchSettings(ssrf_allowed_networks=["127.0.0.0/8"])
    result, _ = _fetch(http, site.url("/page"))
    assert result.outcome is Outcome.OK
    assert result.render is not None
    assert result.render.requests >= 2
