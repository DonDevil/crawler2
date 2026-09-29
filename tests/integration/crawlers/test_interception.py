"""P6 filter behind the P4 browser interception hook, in real Chromium (test plan #12).

A fixture page loads a first-party script, a third-party "ad" script and a
tracker pixel. The filter blocks the ad and tracker sub-requests, never the
page itself, and the per-page aggregate reaches the fetch result.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from antipiracy_contracts.models.web import UrlRef

from crawler2.core.configuration.settings import BrowserSettings
from crawler2.crawlers.browser import BrowserFetcher
from crawler2.crawlers.model import FetchRequest, Outcome
from crawler2.filtering.intercept import FilterInterceptor
from crawler2.filtering.model import Classification, Party, Policy, Rule, RuleKind, RuleSource
from crawler2.filtering.store import RulesetHolder, SourceImport, publish, store_source
from tests.fixtures.web import Dynamic, FixtureHandler, FixtureSite, Route, serve
from tests.integration.crawlers.conftest import HTTP_SETTINGS, browser_only
from tests.unit.filtering.fakes import MemoryFilterRules

pytestmark = [pytest.mark.integration, browser_only]
HTML = {"Content-Type": "text/html; charset=utf-8"}
JS = {"Content-Type": "application/javascript"}
GIF = {"Content-Type": "image/gif"}


def _page(h: FixtureHandler) -> None:
    port = h.server.server_address[1]  # type: ignore[index]
    body = (
        "<html><body><h1>content</h1>"
        '<script src="/static/app.js"></script>'
        f'<script src="http://localhost:{port}/ads/banner.js"></script>'
        f'<img src="http://localhost:{port}/px/collect.gif">'
        "</body></html>"
    ).encode()
    h.send_response(200)
    for name, value in HTML.items():
        h.send_header(name, value)
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    h.write_body(body)


@pytest.fixture(scope="module")
def page_site() -> Iterator[FixtureSite]:
    """The page on 127.0.0.1; the ad and the pixel on localhost (another site, same server)."""
    routes: dict[str, Route | Dynamic] = {
        "/page": Dynamic(_page),
        "/static/app.js": Route(headers=JS, body=b"window.app=1;"),
        "/ads/banner.js": Route(headers=JS, body=b"window.ad=1;"),
        "/px/collect.gif": Route(headers=GIF, body=b"GIF89a"),
    }
    with serve(routes) as site:
        yield site


def _holder() -> RulesetHolder:
    repo = MemoryFilterRules()
    rules = [
        Rule(
            source=RuleSource.EASYLIST,
            kind=RuleKind.URL_PATTERN,
            pattern="/ads/*",
            classification=Classification.AD,
            confidence=0.95,
            excluded_types=frozenset({"document", "popup"}),
        ),
        Rule(
            source=RuleSource.EASYPRIVACY,
            kind=RuleKind.URL_PATTERN,
            pattern="/px/*",
            classification=Classification.TRACKER,
            confidence=0.95,
            party=Party.THIRD,
            excluded_types=frozenset({"document", "popup"}),
        ),
    ]
    now = datetime.now(UTC)
    revision = store_source(
        repo, SourceImport(RuleSource.EASYLIST, rules, "t", "s", "t", {}), by="it", at=now
    )
    record, _ = publish(repo, [("easylist", revision.revision)], Policy(), by="it", at=now)
    repo.activate(record.ruleset_id, expected=None, by="it", at=now)
    holder = RulesetHolder(repo)
    holder.refresh()
    return holder


def test_filter_blocks_ad_and_tracker_subrequests_in_a_real_browser(page_site: FixtureSite) -> None:
    holder = _holder()
    settings = BrowserSettings(
        navigation_timeout_s=10, settle_s=0.5, contexts=1, blocked_resource_types=[]
    )
    fetcher = BrowserFetcher(settings, HTTP_SETTINGS, interceptor=FilterInterceptor(holder))

    async def run() -> object:
        await fetcher.start()
        try:
            url = page_site.url("/page")
            return await fetcher.fetch(FetchRequest(url, UrlRef.of(url).url_id))
        finally:
            await fetcher.close()

    result = asyncio.run(run())
    assert result.outcome is Outcome.OK  # type: ignore[attr-defined]
    render = result.render  # type: ignore[attr-defined]
    assert render is not None
    counts = dict(render.interceptions)
    assert counts.get("ad:block") == 1
    assert counts.get("tracker:block") == 1
    assert counts.get("unknown:allow", 0) >= 2  # the page itself and its own script
    assert render.ruleset == holder.engine.ruleset_id
    assert {host for host, _ in render.blocked} == {"localhost"}
    assert render.requests_aborted == 2
    assert page_site.state.requests_to("/static/app.js")
