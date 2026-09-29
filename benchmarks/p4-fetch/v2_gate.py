"""V2 side of the P4 exit-gate run: real WorkerRuntime pools through the P3 frontier.

Admits every workload URL (default priority, no capability — the static
default profile, no P6/P7) to a fresh frontier namespace and runs one
http pool (50 slots, like V1's 50 workers) and one browser pool (2 pages,
V1's Playwright semaphore) until the frontier is empty. Politeness is the
frontier's shared domain gate at 0.3 s (V1 ``rate_limit``); retries are
the frontier's (``max_attempts`` 3). Fetchers go through the counting
proxy. Attempts are kept in memory (``--no-record`` semantics): the gate
compares fetching, not storage.

    env/bin/python benchmarks/p4-fetch/v2_gate.py URLS --out FILE
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from antipiracy_contracts.ids import UrlId
from antipiracy_contracts.models.web import FetchAttempt, FetchCapability, HttpValidators, UrlRef
from countproxy import CountingProxy

from crawler2.core.configuration import ExecutionQueue, Settings
from crawler2.core.configuration.settings import PoolSettings
from crawler2.core.observability import Metrics
from crawler2.crawlers.browser import BrowserFetcher
from crawler2.crawlers.health import NetworkHealth
from crawler2.crawlers.http import HttpFetcher
from crawler2.crawlers.model import FetchResult
from crawler2.crawlers.runtime import WorkerRuntime
from crawler2.frontier import Admission
from crawler2.frontier.redis import RedisFrontier, connect_redis

USER_AGENT = "AntiPiracyBot/1.0"
RATE_LIMIT_S = 0.3


@dataclass
class ResultLog:
    results: dict[str, list[FetchResult]] = field(default_factory=dict)

    def latest_validators(self, url_id: UrlId) -> HttpValidators | None:
        return None

    def record(
        self, requested: UrlRef, result: FetchResult, *, started_at: datetime, finished_at: datetime
    ) -> FetchAttempt | None:
        self.results.setdefault(result.requested_url, []).append(result)
        return None


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("urls", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--redis-db", type=int, default=9)
    args = parser.parse_args()
    urls = [u.strip() for u in args.urls.read_text().splitlines() if u.strip()]

    proxy = CountingProxy()
    await proxy.start()
    base = Settings()
    fetch = base.workers.fetch.model_copy(update={"user_agent": USER_AGENT, "proxy": proxy.url})
    browser_settings = base.workers.browser_engine.model_copy(update={"proxy": proxy.url})
    frontier = RedisFrontier(
        connect_redis(base.redis.model_copy(update={"db": args.redis_db})),
        base.frontier.model_copy(update={"default_interval_s": RATE_LIMIT_S}),
        namespace=f"p4gate-{uuid.uuid4().hex[:10]}",
    )
    admitted = [u for u in urls if frontier.admit(Admission(UrlRef.of(u))).accepted]
    log = ResultLog()
    metrics = Metrics(base)

    def pool(queue: ExecutionQueue, fetcher: Any, slots: int) -> WorkerRuntime:
        return WorkerRuntime(
            frontier=frontier,
            queue=queue,
            fetcher=fetcher,
            recorder=log,
            settings=PoolSettings(concurrency=slots, idle_poll_min_s=0.1, idle_poll_max_s=0.5),
            health=NetworkHealth(base.workers.network_health),
            metrics=metrics,
            max_memory_mb=4096,
            recover_every_s=5,
        )

    http = pool(ExecutionQueue.HTTP, HttpFetcher(fetch, max_connections=100), 50)
    browser_fetcher = BrowserFetcher(browser_settings, fetch)
    browser = pool(ExecutionQueue.BROWSER, browser_fetcher, 2)
    started = time.monotonic()
    tasks = [asyncio.ensure_future(http.run()), asyncio.ensure_future(browser.run())]
    while True:
        await asyncio.sleep(1)
        if (await asyncio.to_thread(frontier.stats)).active_tasks == 0:
            break
    http.stop()
    browser.stop()
    await asyncio.gather(*tasks)
    wall = time.monotonic() - started
    stats = frontier.stats()
    frontier.clear()
    await proxy.stop()

    rows = []
    probe_limit = fetch.media_probe_bytes
    for url in admitted:
        attempts = log.results.get(url, [])
        last = attempts[-1] if attempts else None
        rows.append(
            {
                "url": url,
                "ok": bool(last and last.outcome.has_content),
                "capability": last.capability.value if last else None,
                "chain": [f"{a.capability.value}:{a.outcome.value}" for a in attempts],
                "status": last.status if last else None,
                "bytes_read": sum(a.bytes_read for a in attempts),
                "media_body_bytes": max(
                    (a.media.bytes_read for a in attempts if a.media), default=0
                ),
            }
        )
    ok = [r for r in rows if r["ok"]]
    browser_ok = [r for r in ok if r["capability"] == FetchCapability.BROWSER.value]
    chains = [step for r in rows for step in r["chain"]]
    summary = {
        "system": "v2",
        "urls": len(rows),
        "successes": len(ok),
        "success_rate": round(len(ok) / len(rows), 4),
        "upstream_bytes": proxy.counters.upstream_bytes,
        "bytes_per_success": round(proxy.counters.upstream_bytes / len(ok)) if ok else None,
        "browser_successes": len(browser_ok),
        "browser_share": round(len(browser_ok) / len(ok), 4) if ok else None,
        "attempts": len(chains),
        "browser_attempts": sum(1 for s in chains if s.startswith("browser:")),
        "outcomes": {
            o: sum(1 for s in chains if s.endswith(":" + o))
            for o in {s.split(":")[1] for s in chains}
        },
        "media_responses": sum(1 for r in rows if r["media_body_bytes"]),
        "media_body_downloads": sum(1 for r in rows if r["media_body_bytes"] > probe_limit),
        "frontier_counters": stats.counters,
        "browser_pool": vars(browser_fetcher.pool.stats) | {"rss_samples": None},
        "wall_s": round(wall, 1),
    }
    print(json.dumps(summary), flush=True)
    args.out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
