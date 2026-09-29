"""``crawler2-worker``: run one worker pool (http | browser | tor) against the frontier.

Hosts choose pools by configuration (``roles``, ``workers.<pool>.*``);
``processes > 1`` starts that many identical worker processes and waits
for them — a local convenience, not a process manager (restarts belong to
the deployment, P14). No intelligence service is needed (B.5 #1).
"""

from __future__ import annotations

import argparse
import asyncio
import multiprocessing
import signal
import sys
from typing import TYPE_CHECKING

from antipiracy_contracts.ids import UrlId
from antipiracy_contracts.models.web import FetchAttempt, HttpValidators, UrlRef

from crawler2.core.configuration import ExecutionQueue, Settings, WorkerRole
from crawler2.core.identity import WorkerIdentity
from crawler2.core.observability import Metrics, configure_logging, get_logger
from crawler2.crawlers.browser import BrowserFetcher
from crawler2.crawlers.health import NetworkHealth
from crawler2.crawlers.http import HttpFetcher
from crawler2.crawlers.interception import RequestInterceptor
from crawler2.crawlers.model import Fetcher, FetchResult
from crawler2.crawlers.recorder import Recorder, StorageRecorder
from crawler2.crawlers.runtime import WorkerRuntime
from crawler2.crawlers.tor import TorFetcher
from crawler2.filtering.intercept import FilterInterceptor
from crawler2.filtering.store import RulesetHolder
from crawler2.frontier.redis.frontier import RedisFrontier, connect_redis

if TYPE_CHECKING:
    from crawler2.storage.scylla import ScyllaStorage

_log = get_logger("crawlers.cli")
POOLS = {
    "http": (ExecutionQueue.HTTP, WorkerRole.HTTP),
    "browser": (ExecutionQueue.BROWSER, WorkerRole.BROWSER),
    "tor": (ExecutionQueue.TOR, WorkerRole.TOR),
}


class NullRecorder:
    """Records nothing (benchmarks that measure fetching only; never the default)."""

    def latest_validators(self, url_id: UrlId) -> HttpValidators | None:
        return None

    def record(self, requested: UrlRef, result: FetchResult, **_: object) -> FetchAttempt | None:
        return None


def build_fetcher(
    pool: str, settings: Settings, interceptor: RequestInterceptor | None = None
) -> Fetcher:
    w = settings.workers
    if pool == "http":
        return HttpFetcher(w.fetch, max_connections=w.http.concurrency * 2)
    if pool == "browser":
        return BrowserFetcher(w.browser_engine, w.fetch, interceptor=interceptor)
    return TorFetcher(w.fetch, w.tor_network, max_connections=w.tor.concurrency * 2)


def build_recorder(settings: Settings, worker: str, storage: ScyllaStorage) -> Recorder:
    from crawler2.storage.objectstore.s3 import S3ObjectStore

    objects = S3ObjectStore(settings.minio, scratch_dir=settings.scratch_dir)
    return StorageRecorder(
        storage.fetch_attempts,
        storage.pages,
        objects,
        worker=worker,
        interceptions=storage.discovery,
    )


async def _refresh_rules(holder: RulesetHolder, interval_s: float) -> None:
    """P6 hot reload for a browser pool: poll the active ruleset off the event loop."""
    while True:
        await asyncio.sleep(interval_s)
        await asyncio.to_thread(holder.refresh)


async def run_pool(
    pool: str, settings: Settings, *, record: bool = True, max_claims: int | None = None
) -> WorkerRuntime:
    queue, role = POOLS[pool]
    identity = str(WorkerIdentity.create(settings.host_id, role))
    metrics = Metrics(settings)
    frontier = RedisFrontier(
        connect_redis(settings.redis), settings.frontier, namespace=settings.redis.namespace
    )
    storage = None
    if record:
        from crawler2.storage.scylla import ScyllaStorage

        storage = ScyllaStorage.open(settings.scylla, instance=identity)
    recorder = build_recorder(settings, identity, storage) if storage else NullRecorder()
    holder: RulesetHolder | None = None
    interceptor: RequestInterceptor | None = None
    if pool == "browser" and storage is not None and settings.filter.browser_interception:
        holder = RulesetHolder(
            storage.filter_rules,
            name=settings.filter.ruleset_name,
            interval_s=settings.filter.reload_interval_s,
            metrics=metrics,
        )
        await asyncio.to_thread(holder.refresh)
        interceptor = FilterInterceptor(holder)
    pool_settings = getattr(settings.workers, pool)
    runtime = WorkerRuntime(
        frontier=frontier,
        queue=queue,
        fetcher=build_fetcher(pool, settings, interceptor),
        recorder=recorder,
        settings=pool_settings,
        health=NetworkHealth(settings.workers.network_health),
        metrics=metrics,
        max_memory_mb=settings.limits.max_memory_mb,
        recover_every_s=settings.workers.recover_every_s,
    )
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, runtime.stop)
    _log.info("worker_start", pool=pool, worker=identity, concurrency=pool_settings.concurrency)
    refresher = (
        asyncio.create_task(_refresh_rules(holder, settings.filter.reload_interval_s))
        if holder is not None
        else None
    )
    try:
        await runtime.run(max_claims=max_claims)
    finally:
        if refresher is not None:
            refresher.cancel()
    _log.info("worker_stop", pool=pool, stats=vars(runtime.stats))
    return runtime


def _child(pool: str, record: bool, max_claims: int | None) -> None:
    settings = Settings()
    configure_logging(settings)
    asyncio.run(run_pool(pool, settings, record=record, max_claims=max_claims))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crawler2-worker", description=__doc__)
    parser.add_argument("--pool", choices=sorted(POOLS), required=True)
    parser.add_argument("--max-claims", type=int, default=None)
    parser.add_argument(
        "--no-record", action="store_true", help="do not persist attempts (benchmarks only)"
    )
    args = parser.parse_args(argv)
    settings = Settings()
    configure_logging(settings)
    processes = getattr(settings.workers, args.pool).processes
    if processes == 1:
        asyncio.run(
            run_pool(args.pool, settings, record=not args.no_record, max_claims=args.max_claims)
        )
        return 0
    children = [
        multiprocessing.get_context("spawn").Process(
            target=_child, args=(args.pool, not args.no_record, args.max_claims)
        )
        for _ in range(processes)
    ]
    for child in children:
        child.start()

    def forward(signum: int, _frame: object) -> None:
        for child in children:
            if child.pid is not None:
                child.terminate()

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    for child in children:
        child.join()
    return max((child.exitcode or 0) for child in children)


if __name__ == "__main__":
    sys.exit(main())
