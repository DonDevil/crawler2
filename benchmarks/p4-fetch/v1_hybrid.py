"""V1 side of the P4 exit-gate run: V1's own HybridCrawler escalation chain per URL.

Runs under V1's interpreter from a ``git archive`` snapshot (``run.sh gate``).
Each URL goes through ``HybridCrawler._run_engine_plan`` — V1's production
chain (async → scrapling → playwright → selenium → http, with V1's
internal retries, ``max_retries=3``) — exactly as its workers call it,
with the P0 configuration (50 workers, timeout 15 s, UA
``AntiPiracyBot/1.0``, Scrapling enabled) and V1's per-domain politeness
(``rate_limit`` 0.3 s between claims of one domain). Every engine is
routed through the counting proxy; nothing in V1 is modified.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import types
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

sys.path.insert(0, os.getcwd())
sys.path.insert(0, str(Path(__file__).resolve().parent))

import aiohttp
import httpx
from countproxy import CountingProxy
from crawler import scrapling_crawler as scrapling_mod
from crawler import selenium_crawler as selenium_mod
from crawler.hybrid_crawler import HybridCrawler
from crawler.playwright_crawler import async_playwright
from utils.request_headers import get_default_headers

TIMEOUT_S = 15
USER_AGENT = "AntiPiracyBot/1.0"
WORKERS = 50
RATE_LIMIT_S = 0.3
BROWSER_ENGINES = {"playwright", "selenium", "scrapling"}


class _NoFrontier:
    raw = None
    lease_ttl = 90


class DomainGate:
    """V1 ``rate_limit``: successive claims of one domain are >= interval apart."""

    def __init__(self, interval: float) -> None:
        self._interval = interval
        self._next: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def wait(self, url: str) -> None:
        host = urlsplit(url).hostname or ""
        async with self._lock:
            now = time.monotonic()
            slot = max(now, self._next.get(host, 0.0))
            self._next[host] = slot + self._interval
        await asyncio.sleep(max(0.0, slot - time.monotonic()))


def _proxy_everything(hybrid: HybridCrawler, proxy_url: str) -> None:
    engine = hybrid._playwright_engine

    async def start(self: Any) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            proxy={"server": proxy_url},
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-notifications",
            ],
        )

    engine._start_browser = types.MethodType(start, engine)

    original_options = selenium_mod.Options

    class ProxiedOptions(original_options):  # type: ignore[misc,valid-type]
        def __init__(self) -> None:
            super().__init__()
            self.add_argument(f"--proxy-server={proxy_url}")

    selenium_mod.Options = ProxiedOptions
    original_fetcher = scrapling_mod.StealthyFetcher

    class ProxiedFetcher:
        @staticmethod
        def fetch(url: str, **kwargs: Any) -> Any:
            return original_fetcher.fetch(url, proxy=proxy_url, **kwargs)

    scrapling_mod.StealthyFetcher = ProxiedFetcher


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("urls", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    urls = [u.strip() for u in args.urls.read_text().splitlines() if u.strip()]

    proxy = CountingProxy()
    await proxy.start()
    os.environ["HTTP_PROXY"] = os.environ["HTTPS_PROXY"] = proxy.url
    hybrid = HybridCrawler(
        _NoFrontier(),
        concurrency=WORKERS,
        timeout=TIMEOUT_S,
        max_retries=3,
        user_agent=USER_AGENT,
        scrapling_enabled=True,
    )
    _proxy_everything(hybrid, proxy.url)
    gate = DomainGate(RATE_LIMIT_S)
    sem = asyncio.Semaphore(WORKERS)
    rows: list[dict[str, Any]] = []

    async def one(url: str) -> None:
        async with sem:
            await gate.wait(url)
            started = time.monotonic()
            try:
                html, failure, chain, engine = await hybrid._run_engine_plan(url)
            except Exception as exc:  # noqa: BLE001 -- recorded
                html, failure, chain, engine = None, f"{type(exc).__name__}: {exc}", [], "?"
            rows.append(
                {
                    "url": url,
                    "ok": bool(html),
                    "engine": engine,
                    "chain": chain,
                    "latency_s": round(time.monotonic() - started, 3),
                    "error": None if html else (failure or "")[:200],
                }
            )

    headers = get_default_headers(USER_AGENT)
    started = time.monotonic()
    async with (
        aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=WORKERS), trust_env=True
        ) as direct,
        httpx.AsyncClient(
            timeout=TIMEOUT_S,
            follow_redirects=True,
            headers=headers,
            proxy=proxy.url,
            limits=httpx.Limits(max_connections=WORKERS),
        ) as client,
    ):
        hybrid._direct_session = direct
        hybrid._httpx_client = client
        await asyncio.gather(*(one(u) for u in urls))
        if hybrid._playwright_ready:
            await hybrid._playwright_engine._stop_browser()
    wall = time.monotonic() - started
    await proxy.stop()

    ok = [r for r in rows if r["ok"]]
    browser_ok = [r for r in ok if r["engine"] in BROWSER_ENGINES]
    attempts = [e for r in rows for e in r["chain"]]
    summary = {
        "system": "v1",
        "urls": len(rows),
        "successes": len(ok),
        "success_rate": round(len(ok) / len(rows), 4),
        "upstream_bytes": proxy.counters.upstream_bytes,
        "bytes_per_success": round(proxy.counters.upstream_bytes / len(ok)) if ok else None,
        "browser_successes": len(browser_ok),
        "browser_share": round(len(browser_ok) / len(ok), 4) if ok else None,
        "attempts": len(attempts),
        "browser_attempts": sum(1 for e in attempts if e in BROWSER_ENGINES),
        "engine_of_success": {
            e: sum(1 for r in ok if r["engine"] == e) for e in {r["engine"] for r in ok}
        },
        "wall_s": round(wall, 1),
    }
    print(json.dumps(summary), flush=True)
    args.out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
