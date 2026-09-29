"""Per-engine evaluation of the V1 fetch engines on the P0 seed set (P4 audit, D14).

Runs under **V1's own interpreter** from a ``git archive`` snapshot of V1
(``run.sh v1-engines``); V1 files are never modified. The only
harness-side interventions are routing every engine through the counting
proxy (``countproxy.py``) -- aiohttp via ``trust_env``, httpx/Playwright/
Selenium/Scrapling via their proxy options -- and ``max_retries=1`` so one
row is one fetch attempt (V1's internal retry loops are D4, measured
separately by the hybrid baseline).

Each engine fetches every URL once, with V1's own per-engine concurrency
caps from ``HybridCrawler`` (async 50 via the P0 setting, http 10,
scrapling 3, playwright 2, selenium 1) and V1's timeout (15 s) and user
agent (``AntiPiracyBot/1.0``, the P0 config value).

Output: one JSON document with per-engine summaries and per-URL rows.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import statistics
import sys
import threading
import time
from pathlib import Path
from typing import Any

import psutil

sys.path.insert(0, os.getcwd())  # the V1 snapshot (run.sh cd's into it)
sys.path.insert(0, str(Path(__file__).resolve().parent))

import aiohttp
import httpx
from core.crawler_router import CrawlerRouter
from core.failure_classifier import classify_failure
from countproxy import CountingProxy
from crawler import scrapling_crawler as scrapling_mod
from crawler import selenium_crawler as selenium_mod
from crawler.async_crawler import AsyncCrawler
from crawler.http_crawler import HTTPCrawler
from crawler.playwright_crawler import PlaywrightCrawler, async_playwright
from utils.request_headers import get_default_headers

TIMEOUT_S = 15
USER_AGENT = "AntiPiracyBot/1.0"
CONCURRENCY = {"async": 50, "http": 10, "scrapling": 3, "playwright": 2, "selenium": 1}
BROWSER_ENGINES = {"playwright", "selenium", "scrapling"}


class _NoFrontier:
    raw = None
    lease_ttl = 90


class RssSampler:
    """Peak/mean RSS of this process plus all descendants (browsers, drivers)."""

    def __init__(self, interval_s: float = 0.25) -> None:
        self._interval = interval_s
        self._stop = threading.Event()
        self.samples: list[float] = []
        self.max_children = 0
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        me = psutil.Process()
        while not self._stop.is_set():
            total = 0
            try:
                procs = [me, *me.children(recursive=True)]
                self.max_children = max(self.max_children, len(procs) - 1)
                for proc in procs:
                    with contextlib.suppress(psutil.Error):
                        total += proc.memory_info().rss
            except psutil.Error:
                pass
            self.samples.append(total / 2**20)
            self._stop.wait(self._interval)

    def __enter__(self) -> RssSampler:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2)


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


async def _measure(
    engine: str, urls: list[str], fetch_one: Any, proxy: CountingProxy
) -> dict[str, Any]:
    sem = asyncio.Semaphore(CONCURRENCY[engine])
    router = CrawlerRouter(allow_scrapling=False)
    rows: list[dict[str, Any]] = []

    async def run(url: str) -> None:
        async with sem:
            started = time.monotonic()
            try:
                html, error = await asyncio.wait_for(fetch_one(url), TIMEOUT_S * 4 + 30)
            except TimeoutError:
                html, error = None, "harness deadline exceeded"
            except Exception as exc:  # noqa: BLE001 -- recorded, not raised
                html, error = None, f"{type(exc).__name__}: {exc}"
            elapsed = time.monotonic() - started
            row: dict[str, Any] = {
                "url": url,
                "ok": bool(html),
                "latency_s": round(elapsed, 3),
                "html_chars": len(html) if html else 0,
            }
            if html and engine in {"async", "http"}:
                row["v1_needs_browser"] = router.needs_browser_upgrade(url, html=html)
            if not html:
                row["error"] = (error or "empty")[:300]
                row["v1_category"] = classify_failure(error).name
            rows.append(row)

    bytes_before, conns_before = proxy.counters.snapshot()
    started = time.monotonic()
    with RssSampler() as rss:
        await asyncio.gather(*(run(u) for u in urls))
    wall = time.monotonic() - started
    bytes_after, conns_after = proxy.counters.snapshot()

    ok = [r for r in rows if r["ok"]]
    latencies = [r["latency_s"] for r in ok]
    categories: dict[str, int] = {}
    for r in rows:
        if not r["ok"]:
            categories[r["v1_category"]] = categories.get(r["v1_category"], 0) + 1
    upstream = bytes_after - bytes_before
    return {
        "engine": engine,
        "concurrency": CONCURRENCY[engine],
        "urls": len(rows),
        "successes": len(ok),
        "success_rate": round(len(ok) / len(rows), 4) if rows else None,
        "v1_needs_browser_on_success": sum(1 for r in ok if r.get("v1_needs_browser")),
        "latency_s_median": _percentile(latencies, 0.5),
        "latency_s_p95": _percentile(latencies, 0.95),
        "latency_s_mean": round(statistics.fmean(latencies), 3) if latencies else None,
        "timeouts": sum(1 for r in rows if r.get("v1_category") == "TIMEOUT"),
        "failure_categories": categories,
        "wall_s": round(wall, 1),
        "upstream_bytes": upstream,
        "bytes_per_success": round(upstream / len(ok)) if ok else None,
        "proxy_connections": conns_after - conns_before,
        "rss_mb_peak_tree": round(max(rss.samples), 1) if rss.samples else None,
        "rss_mb_mean_tree": round(statistics.fmean(rss.samples), 1) if rss.samples else None,
        "max_child_processes": rss.max_children,
        "rows": sorted(rows, key=lambda r: r["url"]),
    }


async def eval_async(urls: list[str], proxy: CountingProxy) -> dict[str, Any]:
    os.environ["HTTP_PROXY"] = os.environ["HTTPS_PROXY"] = proxy.url
    crawler = AsyncCrawler(_NoFrontier(), timeout=TIMEOUT_S, max_retries=1, user_agent=USER_AGENT)
    connector = aiohttp.TCPConnector(limit=CONCURRENCY["async"])
    async with aiohttp.ClientSession(connector=connector, trust_env=True) as session:
        return await _measure("async", urls, lambda u: crawler.fetch(session, u), proxy)


async def eval_http(urls: list[str], proxy: CountingProxy) -> dict[str, Any]:
    crawler = HTTPCrawler(_NoFrontier(), timeout=TIMEOUT_S, max_retries=1, user_agent=USER_AGENT)
    async with httpx.AsyncClient(
        timeout=TIMEOUT_S,
        follow_redirects=True,
        headers=get_default_headers(USER_AGENT),
        proxy=proxy.url,
    ) as client:
        return await _measure("http", urls, lambda u: crawler.fetch(client, u), proxy)


class _ProxiedPlaywright(PlaywrightCrawler):
    """V1's launch arguments verbatim, plus the counting proxy."""

    proxy_url = ""
    startup_s = 0.0

    async def _start_browser(self) -> None:
        started = time.monotonic()
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            proxy={"server": self.proxy_url},
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-notifications",
            ],
        )
        self.startup_s = time.monotonic() - started


async def eval_playwright(urls: list[str], proxy: CountingProxy) -> dict[str, Any]:
    crawler = _ProxiedPlaywright(
        _NoFrontier(), timeout=TIMEOUT_S, max_retries=1, user_agent=USER_AGENT
    )
    crawler.proxy_url = proxy.url
    await crawler._start_browser()
    try:
        result = await _measure("playwright", urls, crawler.fetch, proxy)
    finally:
        await crawler._stop_browser()
    result["browser_startup_s"] = round(crawler.startup_s, 3)
    return result


async def eval_selenium(urls: list[str], proxy: CountingProxy) -> dict[str, Any]:
    original = selenium_mod.Options

    class ProxiedOptions(original):  # type: ignore[misc,valid-type]
        def __init__(self) -> None:
            super().__init__()
            self.add_argument(f"--proxy-server={proxy.url}")

    selenium_mod.Options = ProxiedOptions
    try:
        crawler = selenium_mod.SeleniumCrawler(
            _NoFrontier(), timeout=TIMEOUT_S, max_retries=1, user_agent=USER_AGENT
        )
        startups = []
        for _ in range(3):  # driver start cost, measured apart from page loads
            started = time.monotonic()
            driver = await asyncio.to_thread(crawler._make_driver)
            startups.append(time.monotonic() - started)
            await asyncio.to_thread(driver.quit)
        result = await _measure("selenium", urls, crawler.fetch, proxy)
    finally:
        selenium_mod.Options = original
    result["browser_startup_s"] = round(statistics.median(startups), 3)
    result["pages_per_browser_process"] = 1
    return result


async def eval_scrapling(urls: list[str], proxy: CountingProxy) -> dict[str, Any]:
    original = scrapling_mod.StealthyFetcher

    class ProxiedFetcher:
        @staticmethod
        def fetch(url: str, **kwargs: Any) -> Any:
            return original.fetch(url, proxy=proxy.url, **kwargs)

    scrapling_mod.StealthyFetcher = ProxiedFetcher
    try:
        crawler = scrapling_mod.ScraplingCrawler(
            _NoFrontier(), timeout=TIMEOUT_S, max_retries=1, user_agent=USER_AGENT
        )
        return await _measure("scrapling", urls, crawler.fetch, proxy)
    finally:
        scrapling_mod.StealthyFetcher = original


EVALUATORS = {
    "async": eval_async,
    "http": eval_http,
    "playwright": eval_playwright,
    "selenium": eval_selenium,
    "scrapling": eval_scrapling,
}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("urls", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--engines", default=",".join(EVALUATORS))
    args = parser.parse_args()
    urls = [u.strip() for u in args.urls.read_text().splitlines() if u.strip()]

    proxy = CountingProxy()
    await proxy.start()
    results = []
    try:
        for engine in args.engines.split(","):
            print(f"[v1-engines] {engine}: {len(urls)} urls", flush=True)
            results.append(await EVALUATORS[engine](urls, proxy))
            summary = {k: v for k, v in results[-1].items() if k != "rows"}
            print(json.dumps(summary), flush=True)
    finally:
        await proxy.stop()
    args.out.write_text(
        json.dumps(
            {
                "workload": str(args.urls),
                "timeout_s": TIMEOUT_S,
                "user_agent": USER_AGENT,
                "proxy_errors": proxy.counters.errors,
                "engines": results,
            },
            indent=1,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
