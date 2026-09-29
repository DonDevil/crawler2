"""Playwright browser pool and browser fetcher (P4 design §12, D14).

One Chromium process per worker process, a few long-lived contexts, a
fresh page per attempt. Contexts are replaced after ``recycle_pages``
pages; the browser is drained and relaunched after
``browser_recycle_pages`` pages, when its process tree exceeds
``browser_rss_limit_mb``, or when it died. A dead browser never takes the
worker down: the attempt in flight ends ``fetcher_crash`` and the next
page gets a new browser. Navigation (goto + settle) has one deadline.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace

import psutil
from antipiracy_contracts.models.web import FetchCapability, HttpValidators
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Request,
    Response,
    Route,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from crawler2.core.configuration.settings import BrowserSettings, HttpFetchSettings
from crawler2.core.observability import get_logger
from crawler2.crawlers import signals
from crawler2.crawlers.classify import classify_exception, error_info
from crawler2.crawlers.http import HttpFetcher
from crawler2.crawlers.interception import (
    AllowAll,
    InterceptAction,
    InterceptedRequest,
    RequestInterceptor,
)
from crawler2.crawlers.model import (
    BodyKind,
    Expectation,
    FetcherHealth,
    FetcherState,
    FetcherUnavailableError,
    FetchRequest,
    FetchResult,
    Hop,
    Outcome,
    RenderMetrics,
    Timings,
)

_log = get_logger("crawlers.browser")
_RSS_CHECK_EVERY = 10
_CRASH_MARKERS = ("target page, context or browser has been closed", "browser has been closed")


def process_tree_rss_mb() -> float:
    """RSS of every descendant of this process (Playwright driver + Chromium)."""
    total = 0
    for child in psutil.Process().children(recursive=True):
        with contextlib.suppress(psutil.Error):
            total += child.memory_info().rss
    return total / 2**20


@dataclass
class _Context:
    context: BrowserContext
    generation: int
    served: int = 0
    in_flight: int = 0
    retired: bool = False


@dataclass
class PoolStats:
    browser_launches: int = 0
    browser_restarts: int = 0
    """Relaunches after a crash (recycles are counted separately)."""
    browser_recycles: int = 0
    contexts_created: int = 0
    contexts_closed: int = 0
    pages_opened: int = 0
    pages_closed: int = 0
    last_rss_mb: float = 0.0
    rss_samples: list[tuple[int, float]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PageLease:
    page: Page
    context_pages: int
    browser_pages: int
    acquire_s: float


class BrowserPool:
    def __init__(self, settings: BrowserSettings) -> None:
        self._s = settings
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._generation = 0
        self._contexts: list[_Context] = []
        self._lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(settings.contexts * settings.pages_per_context)
        self._in_flight = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._browser_pages = 0
        self._dead = False
        self._recycle_due = False
        self._unavailable: str | None = None
        self.stats = PoolStats()

    @property
    def capacity(self) -> int:
        return self._s.contexts * self._s.pages_per_context

    def health(self) -> FetcherHealth:
        if self._unavailable is not None:
            return FetcherHealth(FetcherState.UNAVAILABLE, self._unavailable)
        if self._playwright is None:
            return FetcherHealth(FetcherState.UNAVAILABLE, "not started")
        if self._dead:
            return FetcherHealth(FetcherState.DEGRADED, "browser died; relaunch pending")
        return FetcherHealth(FetcherState.READY)

    def browser_pid(self) -> int | None:
        """PID of the Chromium main process (for chaos tests and diagnostics)."""
        for child in psutil.Process().children(recursive=True):
            with contextlib.suppress(psutil.Error):
                cmd = child.cmdline()
                if cmd and "--type=" not in " ".join(cmd) and "chrom" in child.name().lower():
                    return int(child.pid)
        return None

    async def start(self) -> None:
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        async with self._lock:
            await self._launch()

    async def _launch(self) -> None:
        assert self._playwright is not None  # noqa: S101
        last: BaseException | None = None
        for attempt in range(self._s.relaunch_attempts):
            try:
                browser = await self._playwright.chromium.launch(
                    headless=self._s.headless,
                    timeout=self._s.launch_timeout_s * 1000,
                    proxy={"server": self._s.proxy} if self._s.proxy else None,
                    args=["--disable-dev-shm-usage", "--disable-gpu", "--mute-audio"],
                )
            except PlaywrightError as exc:
                last = exc
                _log.warning("browser_launch_failed", attempt=attempt + 1, error=str(exc)[:200])
                await asyncio.sleep(0.5 * 2**attempt)
                continue
            self._generation += 1
            generation = self._generation
            browser.on("disconnected", self._disconnect_handler(generation))
            self._browser = browser
            self._dead = False
            self._recycle_due = False
            self._browser_pages = 0
            self._unavailable = None
            self.stats.browser_launches += 1
            return
        self._unavailable = f"browser cannot be launched: {last}"[:200]
        raise FetcherUnavailableError(self._unavailable)

    def _disconnect_handler(self, generation: int) -> Callable[[Browser], None]:
        return lambda _browser: self._on_disconnected(generation)

    def _on_disconnected(self, generation: int) -> None:
        if generation == self._generation and not self._recycle_due:
            self._dead = True
            _log.warning("browser_disconnected", generation=generation)

    async def _close_browser(self) -> None:
        for ctx in self._contexts:
            with contextlib.suppress(PlaywrightError, TimeoutError):
                await asyncio.wait_for(ctx.context.close(), self._s.operation_timeout_s)
            self.stats.contexts_closed += 1
        self._contexts = []
        if self._browser is not None:
            browser, self._browser = self._browser, None
            with contextlib.suppress(PlaywrightError, TimeoutError):
                await asyncio.wait_for(browser.close(), self._s.operation_timeout_s)

    async def _ensure_browser(self) -> None:
        """Called under the lock: relaunch when dead or due for recycling (after draining)."""
        if self._browser is not None and not self._dead and not self._recycle_due:
            return
        crashed = self._dead or self._browser is None
        if not crashed:
            # Drain: no new page starts while we hold the lock; wait for the running ones.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._idle.wait(), self._s.navigation_timeout_s)
        await self._close_browser()
        if crashed and self.stats.browser_launches:
            self.stats.browser_restarts += 1
        elif not crashed:
            self.stats.browser_recycles += 1
        self._dead = True  # a disconnect from the closing browser must not re-flag the new one
        await self._launch()

    async def _context_for_page(self) -> _Context:
        await self._ensure_browser()
        assert self._browser is not None  # noqa: S101
        for ctx in self._contexts:
            if not ctx.retired and ctx.in_flight < self._s.pages_per_context:
                return ctx
        live = [c for c in self._contexts if not c.retired]
        if len(live) >= self._s.contexts:  # all busy (cannot happen with the slot semaphore)
            return min(live, key=lambda c: c.in_flight)
        context = await asyncio.wait_for(
            self._browser.new_context(java_script_enabled=True, accept_downloads=False),
            self._s.operation_timeout_s,
        )
        ctx = _Context(context=context, generation=self._generation)
        self._contexts.append(ctx)
        self.stats.contexts_created += 1
        return ctx

    @contextlib.asynccontextmanager
    async def page(self) -> AsyncIterator[PageLease]:
        started = time.monotonic()
        try:
            await asyncio.wait_for(self._slots.acquire(), self._s.acquire_timeout_s)
        except TimeoutError as exc:
            raise FetcherUnavailableError("no browser page slot within acquire timeout") from exc
        ctx: _Context | None = None
        page: Page | None = None
        try:
            async with self._lock:
                ctx = await self._context_for_page()
                page = await asyncio.wait_for(ctx.context.new_page(), self._s.operation_timeout_s)
                ctx.in_flight += 1
                ctx.served += 1
                self._in_flight += 1
                self._idle.clear()
                self._browser_pages += 1
                self.stats.pages_opened += 1
                if ctx.served >= self._s.recycle_pages:
                    ctx.retired = True
            yield PageLease(page, ctx.served, self._browser_pages, time.monotonic() - started)
        finally:
            if page is not None and ctx is not None:
                await self._release(ctx, page)
            self._slots.release()

    async def _release(self, ctx: _Context, page: Page) -> None:
        with contextlib.suppress(PlaywrightError, TimeoutError):
            await asyncio.wait_for(page.close(), self._s.operation_timeout_s)
        self.stats.pages_closed += 1
        ctx.in_flight -= 1
        self._in_flight -= 1
        if ctx.retired and ctx.in_flight == 0 and ctx in self._contexts:
            self._contexts.remove(ctx)
            with contextlib.suppress(PlaywrightError, TimeoutError):
                await asyncio.wait_for(ctx.context.close(), self._s.operation_timeout_s)
            self.stats.contexts_closed += 1
        if self._in_flight == 0:
            self._idle.set()
        if self._browser_pages >= self._s.browser_recycle_pages:
            self._recycle_due = True
        if self.stats.pages_closed % _RSS_CHECK_EVERY == 0:
            rss = await asyncio.to_thread(process_tree_rss_mb)
            self.stats.last_rss_mb = rss
            self.stats.rss_samples.append((self.stats.pages_closed, round(rss, 1)))
            if rss > self._s.browser_rss_limit_mb:
                _log.info(
                    "browser_rss_recycle", rss_mb=round(rss), limit=self._s.browser_rss_limit_mb
                )
                self._recycle_due = True

    def mark_dead(self) -> None:
        self._dead = True

    async def close(self) -> None:
        async with self._lock:
            self._recycle_due = True
            await self._close_browser()
        if self._playwright is not None:
            with contextlib.suppress(PlaywrightError, TimeoutError):
                await asyncio.wait_for(self._playwright.stop(), self._s.operation_timeout_s)
            self._playwright = None


class _PageObserver:
    """Per-page request accounting and interception."""

    def __init__(self, settings: BrowserSettings, interceptor: RequestInterceptor) -> None:
        self._blocked_types = frozenset(settings.blocked_resource_types)
        self._interceptor = interceptor
        self.requests = 0
        self.aborted = 0
        self.media = 0
        self.transferred = 0
        self.crashed = False
        self._sizes: list[asyncio.Task[None]] = []

    async def route(self, route: Route) -> None:
        request = route.request
        self.requests += 1
        kind = request.resource_type
        if kind == "media":
            self.media += 1
        navigation = request.is_navigation_request()
        if kind in self._blocked_types and not navigation:
            self.aborted += 1
            await route.abort()
            return
        decision = self._interceptor.decide(
            InterceptedRequest(
                url=request.url,
                resource_type=kind,
                is_navigation=navigation,
                frame_url=request.frame.url if not navigation else "",
                method=request.method,
            )
        )
        if decision.action is InterceptAction.BLOCK:
            self.aborted += 1
            await route.abort()
            return
        await route.continue_()

    def on_finished(self, request: Request) -> None:
        self._sizes.append(asyncio.ensure_future(self._size(request)))

    async def _size(self, request: Request) -> None:
        with contextlib.suppress(PlaywrightError):
            sizes = await request.sizes()
            self.transferred += sizes["responseBodySize"] + sizes["responseHeadersSize"]

    async def settle(self, timeout: float) -> None:
        if self._sizes:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.gather(*self._sizes), timeout)
            for task in self._sizes:
                task.cancel()


async def _redirects(response: Response) -> tuple[Hop, ...]:
    chain: list[Hop] = []
    request = response.request
    previous = request.redirected_from
    while previous is not None:
        prior = await previous.response()
        if prior is not None:
            chain.append(Hop(prior.status, request.url))
        request = previous
        previous = request.redirected_from
    return tuple(reversed(chain))


class BrowserFetcher:
    """Renders a page in the pool; media URLs are probed over HTTP, never loaded (D2)."""

    def __init__(
        self,
        settings: BrowserSettings,
        http_settings: HttpFetchSettings,
        *,
        interceptor: RequestInterceptor | None = None,
    ) -> None:
        self._s = settings
        self._http_settings = http_settings
        self._interceptor = interceptor or AllowAll()
        self.pool = BrowserPool(settings)
        self._prober = HttpFetcher(http_settings, max_connections=4)

    @property
    def capability(self) -> FetchCapability:
        return FetchCapability.BROWSER

    @property
    def total_timeout_s(self) -> float:
        return (
            self._s.acquire_timeout_s + self._s.navigation_timeout_s + self._s.operation_timeout_s
        )

    async def start(self) -> None:
        await self.pool.start()
        await self._prober.start()

    def health(self) -> FetcherHealth:
        return self.pool.health()

    async def close(self) -> None:
        await self.pool.close()
        await self._prober.close()

    async def fetch(self, request: FetchRequest) -> FetchResult:
        if request.expect is Expectation.MEDIA_PROBE or signals.has_media_extension(request.url):
            return await self._prober.fetch(request)
        started = time.monotonic()
        observer = _PageObserver(self._s, self._interceptor)
        lease: PageLease | None = None
        try:
            async with self.pool.page() as lease:
                result = await self._render(request, lease, observer, started)
                await observer.settle(1.0)
        except FetcherUnavailableError:
            raise
        except (PlaywrightError, TimeoutError) as exc:
            result = self._failure(request, exc, observer)
        render = RenderMetrics(
            requests=observer.requests,
            requests_aborted=observer.aborted,
            media_requests=observer.media,
            transferred_bytes=observer.transferred,
            context_pages=lease.context_pages if lease else 0,
            browser_pages=lease.browser_pages if lease else 0,
            browser_restarts=self.pool.stats.browser_restarts,
        )
        timings = Timings(
            total_s=round(time.monotonic() - started, 4),
            acquire_s=round(lease.acquire_s, 4) if lease else None,
            navigation_s=result.timings.navigation_s,
            dom_ready_s=result.timings.dom_ready_s,
        )
        return replace(result, render=render, timings=timings, bytes_read=observer.transferred)

    def _failure(
        self, request: FetchRequest, exc: BaseException, observer: _PageObserver
    ) -> FetchResult:
        text = str(exc).lower()
        if observer.crashed or any(marker in text for marker in _CRASH_MARKERS):
            if "browser has been closed" in text:
                self.pool.mark_dead()
            outcome = Outcome.FETCHER_CRASH
        elif isinstance(exc, PlaywrightTimeoutError | TimeoutError):
            outcome = Outcome.TIMEOUT
        else:
            outcome = classify_exception(exc)
        return FetchResult(
            outcome=outcome,
            capability=FetchCapability.BROWSER,
            requested_url=request.url,
            error=error_info(exc),
        )

    async def _render(
        self, request: FetchRequest, lease: PageLease, observer: _PageObserver, started: float
    ) -> FetchResult:
        page = lease.page
        page.on("crash", lambda _p: setattr(observer, "crashed", True))
        page.on("requestfinished", observer.on_finished)
        page.on("popup", lambda popup: asyncio.ensure_future(popup.close()))
        await page.route("**/*", observer.route)
        deadline = time.monotonic() + self._s.navigation_timeout_s
        nav_started = time.monotonic()
        response = await page.goto(
            request.url, wait_until="domcontentloaded", timeout=self._s.navigation_timeout_s * 1000
        )
        dom_ready = time.monotonic() - nav_started
        settle = min(self._s.settle_s, max(0.0, deadline - time.monotonic()))
        if settle > 0:
            with contextlib.suppress(PlaywrightTimeoutError):
                await page.wait_for_load_state("networkidle", timeout=settle * 1000)
        navigation = time.monotonic() - nav_started
        content = await asyncio.wait_for(page.content(), self._s.operation_timeout_s)
        body = content.encode("utf-8", errors="replace")
        final_url = page.url
        status = response.status if response is not None else 200
        headers: dict[str, str] = {}
        redirects: tuple[Hop, ...] = ()
        if response is not None:
            raw = await asyncio.wait_for(response.all_headers(), self._s.operation_timeout_s)
            headers = {k: v[:500] for k, v in raw.items() if k not in ("set-cookie",)}
            redirects = await _redirects(response)
        timings = Timings(navigation_s=round(navigation, 4), dom_ready_s=round(dom_ready, 4))
        if len(body) > self._s.max_body_bytes:
            return FetchResult(
                outcome=Outcome.TOO_LARGE,
                capability=FetchCapability.BROWSER,
                requested_url=request.url,
                final_url=final_url,
                redirects=redirects,
                status=status,
                headers=headers,
                timings=timings,
            )
        outcome, found = signals.inspect_page(
            status, final_url, body[: self._http_settings.sniff_bytes]
        )
        if outcome is Outcome.NEEDS_JS:  # it was rendered: a script-built page is content here
            outcome = Outcome.OK
        etag, modified = headers.get("etag"), headers.get("last-modified")
        return FetchResult(
            outcome=outcome,
            capability=FetchCapability.BROWSER,
            requested_url=request.url,
            final_url=final_url,
            redirects=redirects,
            status=status,
            headers=headers,
            content_type=headers.get("content-type"),
            body=body,
            body_kind=BodyKind.HTML,
            validators=(
                HttpValidators(etag=etag[:500] if etag else None, last_modified=modified)
                if etag or modified
                else None
            ),
            timings=timings,
            signals=found,
        )
