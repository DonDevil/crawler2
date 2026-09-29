"""``crawler2-discover``: P6 discovery — admission consumer, seed loader, search runs.

``admit`` consumes ``urls.discovered`` in the ``discovery.consumer_group``
group; any number of processes on any hosts may share it (redelivery is
harmless: the frontier deduplicates active tasks and the rows are
idempotent). ``seeds`` and ``search`` admit through the same path.
``--every`` repeats a seed or search run on a static interval (§22 Q3).
No intelligence service is needed (B.5 #1).
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from antipiracy_contracts.events import ContractError
from antipiracy_contracts.events.web import UrlsDiscovered
from antipiracy_contracts.models.web import UrlRef
from pydantic import ValidationError

from crawler2.core.configuration import Settings, WorkerRole
from crawler2.core.identity import WorkerIdentity
from crawler2.core.observability import Metrics, configure_logging, get_logger
from crawler2.discovery.admission import Admitter, LinkAdmissionService, Scope
from crawler2.discovery.search.runner import SearchRunner, http_fetch, read_queries
from crawler2.discovery.seeds import load_seeds
from crawler2.filtering.store import RulesetHolder
from crawler2.frontier.errors import FrontierUnavailableError
from crawler2.storage.errors import StorageError
from crawler2.storage.events.consumer import (
    IdempotentConsumer,
    RedisStreamReader,
    StreamEntry,
    connect_stream_client,
)
from crawler2.storage.events.publisher import stream_name

_log = get_logger("discovery.cli")


@dataclass
class Context:
    settings: Settings
    identity: str
    storage: Any
    redis: Any
    holder: RulesetHolder
    admitter: Admitter
    scope: Scope
    metrics: Metrics


def build(settings: Settings) -> Context:
    from crawler2.frontier.redis.frontier import RedisFrontier, connect_redis
    from crawler2.storage.scylla import ScyllaStorage

    identity = str(WorkerIdentity.create(settings.host_id, WorkerRole.DISCOVERY))
    storage = ScyllaStorage.open(settings.scylla, instance=identity)
    metrics = Metrics(settings)
    redis = connect_redis(settings.redis)
    frontier = RedisFrontier(redis, settings.frontier, namespace=settings.redis.namespace)
    holder = RulesetHolder(
        storage.filter_rules,
        name=settings.filter.ruleset_name,
        interval_s=settings.filter.reload_interval_s,
        metrics=metrics,
    )
    holder.refresh()
    admitter = Admitter(
        frontier=frontier,
        discovery=storage.discovery,
        holder=holder,
        settings=settings.discovery,
        decision_ttl_s=settings.filter.decision_ttl_s,
        metrics=metrics,
    )
    scope = Scope(storage.discovery, refresh_s=settings.discovery.scope_refresh_s)
    return Context(settings, identity, storage, redis, holder, admitter, scope, metrics)


class AdmissionLoop:
    """The ``urls.discovered`` consumer loop (the P5 extraction loop's pattern)."""

    def __init__(
        self,
        reader: RedisStreamReader,
        consumer: IdempotentConsumer[UrlsDiscovered],
        settings: Settings,
        holder: RulesetHolder,
    ) -> None:
        self._reader = reader
        self._consumer = consumer
        self._s = settings.discovery
        self._holder = holder
        self._stopping = False

    def stop(self) -> None:
        self._stopping = True

    def run_once(self) -> int:
        self._holder.maybe_refresh()
        entries = self._reader.claim_stale(
            min_idle_ms=self._s.claim_idle_ms, count=self._s.batch_size
        )
        if not entries:
            entries = self._reader.read(count=self._s.batch_size, block_ms=self._s.block_ms)
        done: list[StreamEntry] = []
        for entry in entries:
            try:
                self._consumer.handle(entry.envelope)
            except (ContractError, ValidationError):
                _log.exception("invalid_event_skipped", entry_id=entry.entry_id)
            except (StorageError, FrontierUnavailableError):
                # Left pending: redelivered by claim_stale once idle (maybe to another host).
                _log.exception("dependency_unavailable", entry_id=entry.entry_id)
                break
            done.append(entry)
        self._reader.ack(done)
        return len(done)

    def run(self, *, max_idle_polls: int | None = None) -> None:
        idle = 0
        while not self._stopping:
            try:
                handled = self.run_once()
            except (StorageError, FrontierUnavailableError):
                _log.exception("poll_failed")
                time.sleep(1.0)
                continue
            idle = 0 if handled else idle + 1
            if max_idle_polls is not None and idle >= max_idle_polls:
                return


def build_admission_loop(ctx: Context) -> AdmissionLoop:
    s = ctx.settings
    service = LinkAdmissionService(
        ctx.admitter,
        ctx.scope,
        ctx.storage.pages,
        ctx.storage.discovery,
        ctx.holder,
        metrics=ctx.metrics,
    )
    reader = RedisStreamReader(
        connect_stream_client(s.redis),
        stream_name(s.events, UrlsDiscovered.EVENT_TYPE, UrlsDiscovered.SCHEMA_MAJOR),
        s.discovery.consumer_group,
        ctx.identity,
    )
    reader.ensure_group()
    consumer = IdempotentConsumer(
        s.discovery.consumer_group,
        UrlsDiscovered,
        service.handle,
        ctx.storage.processed_events,
        metrics=ctx.metrics,
    )
    _log.info("discovery_admission_start", worker=ctx.identity, stream=reader.stream)
    return AdmissionLoop(reader, consumer, s, ctx.holder)


def _repeat(every: float | None, action: Any) -> int:
    stopping = False

    def stop(*_: object) -> None:
        nonlocal stopping
        stopping = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)
    while True:
        action()
        if every is None:
            return 0
        deadline = time.monotonic() + every
        while not stopping and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))
        if stopping:
            return 0


def cmd_admit(args: Any, settings: Settings) -> int:
    ctx = build(settings)
    ctx.metrics.serve()
    loop = build_admission_loop(ctx)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: loop.stop())
    loop.run(max_idle_polls=args.exit_when_idle)
    return 0


def cmd_seeds(args: Any, settings: Settings) -> int:
    ctx = build(settings)
    meta = dict(item.split("=", 1) for item in args.meta)

    def run() -> None:
        report = load_seeds(
            Path(args.file),
            source=args.source,
            metadata=meta,
            admitter=ctx.admitter,
            scope=ctx.scope,
            repo=ctx.storage.discovery,
        )
        print(json.dumps(asdict(report), sort_keys=True), flush=True)

    return _repeat(args.every, run)


def cmd_search(args: Any, settings: Settings) -> int:
    ctx = build(settings)
    queries, digest = read_queries(Path(args.queries))
    fetch = settings.workers.fetch
    fetchers = {"clearnet": http_fetch(settings.search.timeout_s, fetch.user_agent, fetch.proxy)}
    if settings.workers.tor_network.socks_proxy:
        proxy = settings.workers.tor_network.socks_proxy
        fetchers["tor"] = http_fetch(settings.search.timeout_s, fetch.user_agent, proxy)
    runner = SearchRunner(
        settings.search,
        fetchers=fetchers,
        admitter=ctx.admitter,
        scope=ctx.scope,
        repo=ctx.storage.discovery,
        metrics=ctx.metrics,
    )

    def run() -> None:
        for report in runner.run(queries):
            doc = asdict(report) | {"query_file_sha256": digest}
            print(json.dumps(doc, sort_keys=True), flush=True)

    return _repeat(args.every, run)


def cmd_decisions(args: Any, settings: Settings) -> int:
    from crawler2.storage.scylla import ScyllaStorage

    storage = ScyllaStorage.open(settings.scylla, instance="crawler2-discover")
    ref = UrlRef.of(args.url)
    state = storage.discovery.admission_states([ref.url_id]).get(ref.url_id)
    rows = storage.discovery.decisions(ref.url_id)
    print(
        json.dumps(
            {
                "url": ref.url,
                "state": asdict(state) if state else None,
                "decisions": [asdict(r) | {"decision": json.loads(r.decision)} for r in rows],
            },
            indent=2,
            sort_keys=True,
            default=str,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crawler2-discover", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("admit", help="consume urls.discovered and admit links")
    p.add_argument("--exit-when-idle", type=int, default=None, help="stop after N empty polls")
    p.set_defaults(func=cmd_admit)
    p = sub.add_parser("seeds", help="load a seed file and admit its URLs")
    p.add_argument("file")
    p.add_argument("--source", required=True, help="seed source name (provenance)")
    p.add_argument("--meta", action="append", default=[], help="operator metadata k=v")
    p.add_argument("--every", type=float, default=None, help="repeat every S seconds")
    p.set_defaults(func=cmd_seeds)
    p = sub.add_parser("search", help="run operator queries through the search adapters")
    p.add_argument("--queries", required=True, help="operator query file")
    p.add_argument("--every", type=float, default=None, help="repeat every S seconds")
    p.set_defaults(func=cmd_search)
    p = sub.add_parser("decisions", help="show the recorded filter decisions of a URL")
    p.add_argument("url")
    p.set_defaults(func=cmd_decisions)
    args = parser.parse_args(argv)
    settings = Settings()
    configure_logging(settings)
    result: int = args.func(args, settings)
    return result


if __name__ == "__main__":
    sys.exit(main())
