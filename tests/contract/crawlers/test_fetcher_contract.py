"""Fetcher contract suite (P4 design §24): every Fetcher, against the fixture web.

Common cases run for every fetcher; capability-specific ones are marked
with the capabilities they apply to (a browser has no conditional request,
an HTTP client runs no script). Browser fetchers join through
``FETCHERS`` when Chromium is available (``RUN_BROWSER_TESTS=1``).
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager

import psutil
import pytest
from antipiracy_contracts.ids import UrlId
from antipiracy_contracts.models.web import FetchCapability, HttpValidators
from antipiracy_contracts.urls import canonicalize_url

from crawler2.core.configuration.settings import BrowserSettings, HttpFetchSettings, TorSettings
from crawler2.crawlers.http import HttpFetcher
from crawler2.crawlers.model import (
    BodyKind,
    Conditional,
    Fetcher,
    FetcherState,
    FetcherUnavailableError,
    FetchRequest,
    FetchResult,
    Outcome,
)
from crawler2.crawlers.tor import TorFetcher
from tests.fixtures import fetchweb
from tests.fixtures.web import FixtureSite, serve

PROBE = 64 * 1024
SETTINGS = HttpFetchSettings(
    connect_timeout_s=2,
    read_timeout_s=2,
    total_timeout_s=4,
    max_body_bytes=5 * 1024 * 1024,
    media_probe_bytes=PROBE,
)
FULL_DOWNLOAD_GUARD = 4 * 1024 * 1024
"""Server-side bytes allowed for a probe: N + socket buffers, far below 1 GiB."""

BROWSER_ENABLED = os.environ.get("RUN_BROWSER_TESTS") == "1"

FetcherFactory = Callable[[], AbstractAsyncContextManager[Fetcher]]


@pytest.fixture(scope="module")
def site() -> Iterator[FixtureSite]:
    with serve(fetchweb.fetch_routes()) as s:
        yield s


@asynccontextmanager
async def _http() -> AsyncIterator[Fetcher]:
    fetcher = HttpFetcher(SETTINGS)
    await fetcher.start()
    try:
        yield fetcher
    finally:
        await fetcher.close()


@contextmanager
def _socks() -> Iterator[fetchweb.SocksProxy]:
    with fetchweb.socks_proxy() as proxy:
        yield proxy


@asynccontextmanager
async def _tor() -> AsyncIterator[Fetcher]:
    with fetchweb.socks_proxy() as proxy:
        fetcher = TorFetcher(SETTINGS, TorSettings(socks_proxy=proxy.url))
        await fetcher.start()
        try:
            yield fetcher
        finally:
            await fetcher.close()


@asynccontextmanager
async def _browser() -> AsyncIterator[Fetcher]:
    from crawler2.crawlers.browser import BrowserFetcher

    fetcher = BrowserFetcher(
        BrowserSettings(navigation_timeout_s=4, settle_s=0.5, contexts=1),
        SETTINGS,
    )
    await fetcher.start()
    try:
        yield fetcher
    finally:
        await fetcher.close()


FETCHERS: dict[str, FetcherFactory] = {"http": _http, "tor": _tor}
if BROWSER_ENABLED:
    FETCHERS["browser"] = _browser

HTTP_LIKE = {"http", "tor"}


def _request(url: str, **kwargs: object) -> FetchRequest:
    canonical = canonicalize_url(url)
    return FetchRequest(url=url, url_id=UrlId.of(canonical), **kwargs)  # type: ignore[arg-type]


def fetch(factory: FetcherFactory, url: str, **kwargs: object) -> FetchResult:
    async def go() -> FetchResult:
        async with factory() as fetcher:
            return await fetcher.fetch(_request(url, **kwargs))

    return asyncio.run(go())


@pytest.fixture(params=sorted(FETCHERS))
def kind(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def factory(kind: str) -> FetcherFactory:
    return FETCHERS[kind]


def only(kind: str, allowed: set[str]) -> None:
    if kind not in allowed:
        pytest.skip(f"not applicable to {kind}")


# 1 --------------------------------------------------------------------------
def test_basic_success(site: FixtureSite, factory: FetcherFactory, kind: str) -> None:
    result = fetch(factory, site.url("/ok"))
    assert result.outcome is Outcome.OK
    assert result.status == 200
    assert result.final_url == site.url("/ok")
    assert result.body is not None
    assert b"fixture ok" in result.body
    assert result.body_kind is BodyKind.HTML
    assert result.bytes_read > 0
    assert result.timings.total_s > 0
    assert result.capability in set(FetchCapability)


# 2-3 ------------------------------------------------------------------------
def test_redirect(site: FixtureSite, factory: FetcherFactory) -> None:
    result = fetch(factory, site.url("/redirect"))
    assert result.outcome is Outcome.OK
    assert result.final_url == site.url("/ok")
    assert [(h.status, h.location) for h in result.redirects] == [(302, site.url("/ok"))]


def test_redirect_chain(site: FixtureSite, factory: FetcherFactory) -> None:
    result = fetch(factory, site.url("/chain/1"))
    assert result.outcome is Outcome.OK
    assert [h.status for h in result.redirects] == [301, 302, 307]
    assert result.redirects[-1].location == site.url("/ok")


def test_redirect_loop(site: FixtureSite, factory: FetcherFactory) -> None:
    assert fetch(factory, site.url("/loop/a")).outcome is Outcome.REDIRECT_ERROR


def test_redirect_to_unsupported_scheme(
    site: FixtureSite, factory: FetcherFactory, kind: str
) -> None:
    only(kind, HTTP_LIKE)  # Chromium reports this as a generic net::ERR_ABORTED
    assert fetch(factory, site.url("/redirect-ftp")).outcome is Outcome.REDIRECT_ERROR


# 4 --------------------------------------------------------------------------
def test_conditional_request_304(site: FixtureSite, factory: FetcherFactory, kind: str) -> None:
    only(kind, HTTP_LIKE)
    first = fetch(factory, site.url("/etag"))
    assert first.outcome is Outcome.OK
    assert first.validators == HttpValidators(
        etag=fetchweb.ETAG, last_modified=fetchweb.LAST_MODIFIED
    )
    again = fetch(factory, site.url("/etag"), validators=first.validators)
    assert again.outcome is Outcome.NOT_MODIFIED
    assert again.status == 304
    assert again.conditional is Conditional.NOT_MODIFIED
    assert again.body is None
    sent = site.state.requests_to("/etag")[-1].headers
    assert sent["if-none-match"] == fetchweb.ETAG
    assert sent["if-modified-since"] == fetchweb.LAST_MODIFIED


def test_no_fabricated_validators(site: FixtureSite, factory: FetcherFactory, kind: str) -> None:
    only(kind, HTTP_LIKE)
    result = fetch(factory, site.url("/ok"))
    assert result.validators is None
    assert "if-none-match" not in site.state.requests_to("/ok")[-1].headers


# 5-6 ------------------------------------------------------------------------
@pytest.mark.parametrize(("path", "title"), [("/gzip", b"gzip page"), ("/brotli", b"brotli page")])
def test_compressed_bodies(
    site: FixtureSite, factory: FetcherFactory, path: str, title: bytes
) -> None:
    result = fetch(factory, site.url(path))
    assert result.outcome is Outcome.OK
    assert result.body is not None
    assert title in result.body


def test_compression_bomb_is_bounded(site: FixtureSite, factory: FetcherFactory, kind: str) -> None:
    only(kind, HTTP_LIKE)
    rss_before = psutil.Process().memory_info().rss
    result = fetch(factory, site.url("/bomb"))
    assert result.outcome is Outcome.TOO_LARGE
    assert result.body is None
    assert psutil.Process().memory_info().rss - rss_before < 48 * 1024 * 1024


def test_large_page_is_too_large(site: FixtureSite, factory: FetcherFactory, kind: str) -> None:
    only(kind, HTTP_LIKE)
    result = fetch(factory, site.url("/large"))
    assert result.outcome is Outcome.TOO_LARGE
    assert result.body is None


# 7-9 ------------------------------------------------------------------------
def test_js_only_page(site: FixtureSite, factory: FetcherFactory, kind: str) -> None:
    result = fetch(factory, site.url("/js-only"))
    if kind in HTTP_LIKE:
        assert result.outcome is Outcome.NEEDS_JS
        assert result.signals
    else:
        assert result.outcome is Outcome.OK
        assert result.body is not None
        assert b"rendered-by-js" in result.body


def test_captcha_page(site: FixtureSite, factory: FetcherFactory) -> None:
    result = fetch(factory, site.url("/captcha"))
    assert result.outcome is Outcome.CAPTCHA
    assert "g-recaptcha" in result.signals


def test_blocked_pages(site: FixtureSite, factory: FetcherFactory) -> None:
    challenge = fetch(factory, site.url("/blocked"))
    assert challenge.outcome is Outcome.BLOCKED
    assert challenge.status == 403
    limited = fetch(factory, site.url("/ratelimited"))
    assert limited.outcome is Outcome.BLOCKED
    assert limited.status == 429


def test_http_errors_are_responses(site: FixtureSite, factory: FetcherFactory) -> None:
    missing = fetch(factory, site.url("/does-not-exist"))
    assert missing.outcome is Outcome.HTTP_ERROR
    assert missing.status == 404
    assert fetch(factory, site.url("/server-error")).status == 503


# 10 -------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["/media/huge.mp4", "/media/norange.mp4", "/stream/video"])
def test_huge_media_is_probed_not_downloaded(
    site: FixtureSite, factory: FetcherFactory, kind: str, path: str
) -> None:
    only(kind, HTTP_LIKE)
    result = fetch(factory, site.url(path))
    assert result.outcome is Outcome.MEDIA
    assert result.media is not None
    assert result.body is None
    assert result.media.bytes_read <= PROBE
    assert result.media.container == "mp4"
    # The decisive check: what the server actually wrote for this request.
    assert site.state.bytes_sent[path] < FULL_DOWNLOAD_GUARD
    assert result.bytes_read < FULL_DOWNLOAD_GUARD
    sent = site.state.requests_to(path)[-1].headers
    if path.endswith(".mp4"):
        assert sent["range"] == f"bytes=0-{PROBE - 1}"
    if path == "/media/huge.mp4":
        assert result.media.range_honoured
        assert result.media.total_length == fetchweb.HUGE_MEDIA_BYTES


def test_manifest_is_a_small_page(site: FixtureSite, factory: FetcherFactory, kind: str) -> None:
    only(kind, HTTP_LIKE)
    result = fetch(factory, site.url("/master.m3u8"))
    assert result.outcome is Outcome.OK
    assert result.body_kind is BodyKind.MANIFEST
    assert result.body == fetchweb.MANIFEST


# 11-13 ----------------------------------------------------------------------
def test_slowloris_hits_total_deadline(site: FixtureSite, factory: FetcherFactory) -> None:
    started = time.monotonic()
    result = fetch(factory, site.url("/slowloris"))
    assert result.outcome is Outcome.TIMEOUT
    assert time.monotonic() - started < 10


def test_stalled_server_times_out(site: FixtureSite, factory: FetcherFactory) -> None:
    started = time.monotonic()
    assert fetch(factory, site.url("/stall")).outcome is Outcome.TIMEOUT
    assert time.monotonic() - started < 10


def test_cancellation_stops_the_attempt(site: FixtureSite, factory: FetcherFactory) -> None:
    async def go() -> float:
        async with factory() as fetcher:
            task = asyncio.ensure_future(fetcher.fetch(_request(site.url("/stall"))))
            await asyncio.sleep(0.5)
            task.cancel()
            started = time.monotonic()
            with pytest.raises(asyncio.CancelledError):
                await task
            return time.monotonic() - started

    assert asyncio.run(go()) < 1.0


# 14 -------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/malformed", {Outcome.INVALID_RESPONSE}),
        ("/truncated", {Outcome.INVALID_RESPONSE}),
        ("/reset", {Outcome.NETWORK_ERROR, Outcome.INVALID_RESPONSE}),
    ],
)
def test_malformed_responses(
    site: FixtureSite, factory: FetcherFactory, kind: str, path: str, expected: set[Outcome]
) -> None:
    only(kind, HTTP_LIKE)
    assert fetch(factory, site.url(path)).outcome in expected


def test_unreachable_targets(factory: FetcherFactory, kind: str) -> None:
    only(kind, {"http"})
    with contextlib_socket() as port:
        assert fetch(factory, f"http://127.0.0.1:{port}/").outcome is Outcome.NETWORK_ERROR
    assert fetch(factory, "http://does-not-exist.invalid/").outcome is Outcome.DNS_ERROR


# 15 -------------------------------------------------------------------------
def test_resource_cleanup(site: FixtureSite, factory: FetcherFactory) -> None:
    port = int(site.base_url.rsplit(":", 1)[1])
    me = psutil.Process()

    async def go() -> Fetcher:
        async with factory() as fetcher:
            for path in ("/ok", "/redirect", "/media/huge.mp4", "/gzip"):
                await fetcher.fetch(_request(site.url(path)))
        return fetcher

    fetcher = asyncio.run(go())
    assert fetcher.health().state is FetcherState.UNAVAILABLE
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        open_to_site = [c for c in me.net_connections() if c.raddr and c.raddr.port == port]
        if not open_to_site:
            break
        time.sleep(0.1)
    assert not open_to_site
    assert not me.children(recursive=True)


# HTTP-specific --------------------------------------------------------------
def test_cookies_do_not_outlive_an_attempt(site: FixtureSite, kind: str) -> None:
    only(kind, {"http"})

    async def go() -> tuple[FetchResult, FetchResult]:
        async with _http() as fetcher:
            within = await fetcher.fetch(_request(site.url("/cookie/set")))
            later = await fetcher.fetch(_request(site.url("/cookie/check")))
            return within, later

    within, later = asyncio.run(go())
    assert within.body is not None
    assert b"session=abc" in within.body
    assert later.body is not None
    assert b"session=abc" not in later.body


def test_honest_headers(site: FixtureSite, kind: str) -> None:
    only(kind, {"http"})
    fetch(_http, site.url("/ok"))
    headers = site.state.requests_to("/ok")[-1].headers
    assert headers["user-agent"] == SETTINGS.user_agent
    assert not any(name.startswith("sec-fetch") for name in headers)


# Tor-specific ----------------------------------------------------------------
def test_tor_without_proxy_is_unavailable(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    only(kind, {"tor"})
    monkeypatch.delenv("TOR_SOCKS_PROXY", raising=False)
    monkeypatch.delenv("TOR_SOCKS_PORT", raising=False)
    with contextlib_socket() as port:
        fetcher = TorFetcher(SETTINGS, TorSettings(probe_ports=[port]))
        with pytest.raises(FetcherUnavailableError):
            asyncio.run(fetcher.start())


def test_tor_proxy_loss_is_local_not_target(site: FixtureSite, kind: str) -> None:
    only(kind, {"tor"})

    async def go() -> tuple[FetchResult, FetchResult, int]:
        with fetchweb.socks_proxy() as proxy:
            fetcher = TorFetcher(SETTINGS, TorSettings(socks_proxy=proxy.url))
            await fetcher.start()
            ok = await fetcher.fetch(_request(site.url("/ok")))
            used = proxy.connections
        # proxy gone: the next attempt fails locally
        lost = await fetcher.fetch(_request(site.url("/ok")))
        await fetcher.close()
        return ok, lost, used

    ok, lost, used = asyncio.run(go())
    assert ok.outcome is Outcome.OK
    assert used >= 1
    assert lost.outcome is Outcome.PROXY_UNAVAILABLE


@contextmanager
def contextlib_socket() -> Iterator[int]:
    """A port that is bound but not listening: connections are refused."""
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    try:
        yield sock.getsockname()[1]
    finally:
        sock.close()
