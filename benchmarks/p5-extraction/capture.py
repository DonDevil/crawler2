"""Capture the HTML documents of a URL list with the real P4 runtime (P5 design §18, D-2).

Same setup as the P4 gate (``benchmarks/p4-fetch/v2_gate.py``): every URL
is admitted to a fresh frontier namespace and one http pool (50 slots) and
one browser pool (2 pages) run until the frontier is empty. Media is only
probed (P4), so no media bodies are ever downloaded. Response bodies are
written to a local scratch directory (``var/`` is git-ignored, ADR-006
scratch); only the small manifest summary is meant to be committed.

    env/bin/python benchmarks/p5-extraction/capture.py URLS --out var/p5-corpus/capture-1
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from antipiracy_contracts.ids import UrlId
from antipiracy_contracts.models.web import FetchAttempt, HttpValidators, UrlRef

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


class BodyRecorder:
    """Keeps the last response body per requested URL on local scratch."""

    def __init__(self, out: Path) -> None:
        self.out = out
        (out / "bodies").mkdir(parents=True, exist_ok=True)
        self.rows: dict[str, dict[str, Any]] = {}

    def latest_validators(self, url_id: UrlId) -> HttpValidators | None:
        return None

    def record(
        self, requested: UrlRef, result: FetchResult, *, started_at: datetime, finished_at: datetime
    ) -> FetchAttempt | None:
        row: dict[str, Any] = {
            "url": result.requested_url,
            "final_url": result.final_url,
            "status": result.status,
            "outcome": result.outcome.value,
            "capability": result.capability.value,
            "content_type": result.content_type,
            "observed_at": finished_at.isoformat(),
        }
        if result.body is not None and result.status is not None:
            digest = hashlib.sha256(result.body).hexdigest()
            (self.out / "bodies" / digest).write_bytes(result.body)
            row |= {"sha256": digest, "size": len(result.body)}
        self.rows[result.requested_url] = row
        return None


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("urls", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--redis-db", type=int, default=9)
    args = parser.parse_args()
    urls = [u.strip() for u in args.urls.read_text().splitlines() if u.strip()]

    base = Settings()
    fetch = base.workers.fetch.model_copy(update={"user_agent": USER_AGENT})
    frontier = RedisFrontier(
        connect_redis(base.redis.model_copy(update={"db": args.redis_db})),
        base.frontier.model_copy(update={"default_interval_s": RATE_LIMIT_S}),
        namespace=f"p5capture-{uuid.uuid4().hex[:10]}",
    )
    admitted = [u for u in urls if frontier.admit(Admission(UrlRef.of(u))).accepted]
    recorder = BodyRecorder(args.out)
    metrics = Metrics(base)

    def pool(queue: ExecutionQueue, fetcher: Any, slots: int) -> WorkerRuntime:
        return WorkerRuntime(
            frontier=frontier,
            queue=queue,
            fetcher=fetcher,
            recorder=recorder,
            settings=PoolSettings(concurrency=slots, idle_poll_min_s=0.1, idle_poll_max_s=0.5),
            health=NetworkHealth(base.workers.network_health),
            metrics=metrics,
            max_memory_mb=4096,
            recover_every_s=5,
        )

    http = pool(ExecutionQueue.HTTP, HttpFetcher(fetch, max_connections=100), 50)
    browser = pool(ExecutionQueue.BROWSER, BrowserFetcher(base.workers.browser_engine, fetch), 2)
    started = time.monotonic()
    tasks = [asyncio.ensure_future(http.run()), asyncio.ensure_future(browser.run())]
    while True:
        await asyncio.sleep(1)
        if (await asyncio.to_thread(frontier.stats)).active_tasks == 0:
            break
    http.stop()
    browser.stop()
    await asyncio.gather(*tasks)
    frontier.clear()

    rows = [recorder.rows.get(u, {"url": u, "outcome": "none"}) for u in admitted]
    (args.out / "manifest.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    summary = {
        "urls": len(rows),
        "with_body": sum(1 for r in rows if "sha256" in r),
        "html_2xx": sum(
            1
            for r in rows
            if "sha256" in r
            and 200 <= (r["status"] or 0) < 300
            and "html" in (r["content_type"] or "")
        ),
        "wall_s": round(time.monotonic() - started, 1),
    }
    (args.out / "summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
