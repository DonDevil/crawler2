"""``crawler2-media``: the P8 media registry (docs/phases/p08-media-registry/).

``resolve run`` resolves ``media.discovered`` from the outbox (ADR-024). Run
it on one or more hosts: one holds the lease, the others stand by. Stopping
it never affects the crawl. ``resolve status`` exits 0 (ok, warning, never
started), 1 (lag beyond ``media.lag_critical_s``) or 2 (halted in a
retention gap). ``resolve accept-gap`` records a halted gap as accepted loss
and resumes at the oldest retained outbox bucket.

``probe run`` / ``hash run`` work the Redis probe/hash queues on any number
of hosts (per-media leases); ``hash run`` also issues ``encode.requested``
(one logical request per content). ``encode sweep`` re-emits claims whose
outbox write never completed. ``representations run`` keeps
``representation_status`` from the fingerprinter's events. ``rebuild``
re-derives the media read models of one media from the authoritative rows.
``show`` prints what the registry knows about one locator.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from antipiracy_contracts.ids import MediaId
from antipiracy_contracts.models.web import UrlRef
from antipiracy_contracts.ownership import Component, Producer

from crawler2.core.configuration import Settings, WorkerRole
from crawler2.core.identity import WorkerIdentity
from crawler2.core.observability import Metrics, configure_logging
from crawler2.media.encode import EncodeRequester, RepresentationLoop, RepresentationProjection
from crawler2.media.hashing import FullContentHasher
from crawler2.media.probe import (
    HashWorker,
    ProbeMetrics,
    ProbeWorker,
    guarded,
    hash_limits,
    probe_fetcher,
    run_pool,
)
from crawler2.media.resolver import Resolver, ResolverConfig
from crawler2.media.schedule import ProbeScheduler
from crawler2.media.store import ScyllaRegistryStore
from crawler2.media.work import RedisWork


def producer_for(identity: str) -> Producer:
    return Producer(
        service=Component.MEDIA_REGISTRY.service,
        component=Component.MEDIA_REGISTRY,
        instance=identity,
    )


def resolver_config(settings: Settings) -> ResolverConfig:
    m = settings.media
    return ResolverConfig(
        reader=m.reader,
        lease_ttl=timedelta(seconds=m.lease_ttl_s),
        settle=timedelta(seconds=settings.events.relay_settle_s),
        retention=timedelta(seconds=m.outbox_retention_s),
        retention_margin=timedelta(seconds=m.retention_margin_s),
        max_buckets_per_cycle=m.max_buckets_per_cycle,
        lag_warn=timedelta(seconds=m.lag_warn_s),
        lag_critical=timedelta(seconds=m.lag_critical_s),
    )


class Context:
    """Connections of one CLI process (lazy imports keep ``--help`` fast)."""

    def __init__(self, settings: Settings, *, metrics: Metrics | None = None) -> None:
        from crawler2.frontier.redis import connect_redis
        from crawler2.storage.scylla import ScyllaStorage

        self.settings = settings
        self.identity = str(WorkerIdentity.create(settings.host_id, WorkerRole.MEDIA_PROBE))
        self.storage = ScyllaStorage.open(settings.scylla, instance=self.identity)
        self.store = ScyllaRegistryStore(self.storage.session)
        self.redis = connect_redis(settings.redis)
        self.work = RedisWork(self.redis, namespace=settings.redis.namespace)
        self.metrics = metrics

    def requester(self) -> EncodeRequester:
        m = self.settings.media
        return EncodeRequester(
            self.store,
            self.storage.projections,
            self.storage.outbox,
            producer=producer_for(self.identity),
            clock=self.work.now,
            grace=timedelta(seconds=m.claim_emit_grace_s),
            lookback_days=m.sweep_lookback_days,
            metrics=self.metrics,
        )

    def resolver(self, *, schedule: bool = True) -> Resolver:
        sink = None
        if schedule:
            sink = ProbeScheduler(
                self.work,
                reprobe_after=timedelta(seconds=self.settings.media.reprobe_after_s),
                requester=self.requester(),
                metrics=self.metrics,
            )
        return Resolver(
            self.storage.outbox,
            self.store,
            self.storage.media,
            producer=producer_for(self.identity),
            config=resolver_config(self.settings),
            views=self.store,
            sink=sink,
            clock=self.work.now,
            metrics=self.metrics,
        )


def _stopper() -> Any:
    state = {"stop": False}

    def stop(*_: object) -> None:
        state["stop"] = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)
    return lambda: state["stop"]


def _json(doc: Any) -> str:
    return json.dumps(doc, indent=2, default=str, sort_keys=True)


def cmd_resolve_run(args: Any, settings: Settings) -> int:
    metrics = Metrics(settings)
    metrics.serve()
    ctx = Context(settings, metrics=metrics)
    ctx.resolver().run_forever(
        poll_interval_s=settings.media.poll_interval_s,
        retry_max_s=settings.media.retry_max_s,
        stop=_stopper(),
    )
    return 0


def cmd_resolve_status(args: Any, settings: Settings) -> int:
    ctx = Context(settings)
    status = ctx.resolver(schedule=False).status()
    doc = {
        "level": status.level.value,
        "lag_s": round(status.lag_s) if status.lag_s is not None else None,
        "lease_valid": status.lease_valid,
        "state": asdict(status.state) if status.state is not None else None,
        "gaps": [asdict(g) for g in ctx.store.gaps(settings.media.reader)],
    }
    print(_json(doc))
    return status.exit_code


def cmd_resolve_accept_gap(args: Any, settings: Settings) -> int:
    ctx = Context(settings)
    try:
        state = ctx.resolver(schedule=False).accept_gap(by=args.by or ctx.identity, note=args.note)
    except ValueError as exc:
        print(_json({"accepted": False, "error": str(exc)}))
        return 1
    print(_json({"accepted": state is not None, "state": asdict(state) if state else None}))
    return 0 if state is not None else 1


def cmd_probe_run(args: Any, settings: Settings) -> int:
    metrics = Metrics(settings)
    metrics.serve()
    ctx = Context(settings, metrics=metrics)
    m = settings.media
    pm = ProbeMetrics(metrics)
    fetcher = probe_fetcher(
        settings.workers.fetch,
        guarded(m, on_reject=pm.rejected("probe")),
        connections=m.probe_concurrency * 2,
    )
    worker = ProbeWorker(fetcher, ctx.store, ctx.work, worker=ctx.identity, settings=m, metrics=pm)
    stop = _stopper()

    async def main() -> None:
        try:
            await run_pool(
                "probe",
                ctx.work,
                worker.handle,
                concurrency=m.probe_concurrency,
                stop=stop,
                block_s=m.queue_block_s,
            )
        finally:
            await fetcher.close()

    asyncio.run(main())
    return 0


def cmd_hash_run(args: Any, settings: Settings) -> int:
    metrics = Metrics(settings)
    metrics.serve()
    ctx = Context(settings, metrics=metrics)
    m = settings.media
    pm = ProbeMetrics(metrics)
    stop = _stopper()

    async def main() -> None:
        hasher = FullContentHasher(
            guarded(m, on_reject=pm.rejected("hash")), hash_limits(m, settings.workers.fetch)
        )
        worker = HashWorker(
            hasher,
            ctx.store,
            ctx.work,
            worker=ctx.identity,
            settings=m,
            requester=ctx.requester(),
            metrics=pm,
        )
        try:
            await run_pool(
                "hash",
                ctx.work,
                worker.handle,
                concurrency=m.hash_concurrency,
                stop=stop,
                block_s=m.queue_block_s,
            )
        finally:
            await hasher.close()

    asyncio.run(main())
    return 0


def cmd_encode_sweep(args: Any, settings: Settings) -> int:
    """Re-emit encode claims whose outbox write never completed (crash recovery)."""
    ctx = Context(settings)
    requester = ctx.requester()
    if args.once:
        print(_json(asdict(requester.sweep())))
        return 0
    requester.sweep_forever(interval_s=settings.media.sweep_interval_s, stop=_stopper())
    return 0


def cmd_representations_run(args: Any, settings: Settings) -> int:
    """Consume representation.ready / encode.failed (fingerprinter streams) into the
    P1 ``representation_status`` projection."""
    from antipiracy_contracts.events.fingerprinting import EncodeFailed, RepresentationReady

    from crawler2.storage.events.consumer import (
        IdempotentConsumer,
        RedisStreamReader,
        connect_stream_client,
    )
    from crawler2.storage.events.publisher import stream_name

    metrics = Metrics(settings)
    metrics.serve()
    ctx = Context(settings, metrics=metrics)
    m = settings.media
    client = connect_stream_client(settings.redis)
    projection = RepresentationProjection(ctx.storage.projections)
    ready_reader = RedisStreamReader(
        client,
        stream_name(
            settings.events, RepresentationReady.EVENT_TYPE, RepresentationReady.SCHEMA_MAJOR
        ),
        m.consumer_group,
        ctx.identity,
    )
    failed_reader = RedisStreamReader(
        client,
        stream_name(settings.events, EncodeFailed.EVENT_TYPE, EncodeFailed.SCHEMA_MAJOR),
        m.consumer_group,
        ctx.identity,
    )
    store = ctx.storage.processed_events
    pairs: list[Any] = [
        (
            ready_reader,
            IdempotentConsumer(
                m.consumer_group, RepresentationReady, projection.ready, store, metrics=metrics
            ),
        ),
        (
            failed_reader,
            IdempotentConsumer(
                m.consumer_group, EncodeFailed, projection.failed, store, metrics=metrics
            ),
        ),
    ]
    for reader, _ in pairs:
        reader.ensure_group()
    loop = RepresentationLoop(
        pairs, batch_size=m.batch_size, block_ms=m.block_ms, claim_idle_ms=m.claim_idle_ms
    )
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: loop.stop())
    loop.run(max_idle_polls=args.exit_when_idle)
    return 0


def _window(args: Any) -> tuple[datetime, datetime]:
    until = datetime.fromisoformat(args.until) if args.until else datetime.now().astimezone()
    since = datetime.fromisoformat(args.since)
    return since, until


def _media_id(value: str) -> MediaId:
    if value.startswith(("http://", "https://")):
        return MediaId.of_locator(UrlRef.of(value).url)
    return MediaId(value)


def cmd_rebuild(args: Any, settings: Settings) -> int:
    """M1/M4/M5/M6 from M3 → M2 (P2), then the hash side of M5/M6 and the latest view."""
    ctx = Context(settings)
    media_id = _media_id(args.media)
    since, until = _window(args)
    observations = ctx.storage.media.rebuild_media(media_id, since=since, until=until)
    probes, hashes = ctx.store.rebuild(media_id, since=since, until=until)
    print(
        _json(
            {
                "media_id": str(media_id),
                "observations": observations,
                "probes": probes,
                "hashes": hashes,
            }
        )
    )
    return 0


def cmd_show(args: Any, settings: Settings) -> int:
    ctx = Context(settings)
    media_id = _media_id(args.media)
    latest = ctx.store.latest(media_id)
    current = latest.current_content() if latest else None
    record = ctx.storage.media.get(media_id)
    doc = {
        "media_id": str(media_id),
        "media": record.media.model_dump(mode="json") if record else None,
        "latest_probe": json.loads(latest.probe.to_json()) if latest and latest.probe else None,
        "latest_hash": json.loads(latest.hash.to_json()) if latest and latest.hash else None,
        "current_content": current.model_dump(mode="json") if current else None,
    }
    print(_json(doc))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crawler2-media", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    resolve = sub.add_parser("resolve", help="media.discovered → observations").add_subparsers(
        dest="action", required=True
    )
    resolve.add_parser("run", help="resolve from the outbox (one lease holder)").set_defaults(
        func=cmd_resolve_run
    )
    resolve.add_parser("status", help="checkpoint, lag, lease and gaps").set_defaults(
        func=cmd_resolve_status
    )
    p = resolve.add_parser("accept-gap", help="record a halted gap as loss and resume")
    p.add_argument("--note", required=True, help="why the loss is accepted (stored)")
    p.add_argument("--by", default=None, help="operator name (default: this process identity)")
    p.set_defaults(func=cmd_resolve_accept_gap)
    sub.add_parser("probe", help="probe pool").add_subparsers(
        dest="action", required=True
    ).add_parser("run", help="work the probe queue").set_defaults(func=cmd_probe_run)
    sub.add_parser("hash", help="full-content hash pool").add_subparsers(
        dest="action", required=True
    ).add_parser("run", help="work the hash queue").set_defaults(func=cmd_hash_run)
    encode = sub.add_parser("encode", help="encode requester").add_subparsers(
        dest="action", required=True
    )
    p = encode.add_parser("sweep", help="re-emit claims whose outbox write never completed")
    p.add_argument("--once", action="store_true", help="one sweep, print the report, exit")
    p.set_defaults(func=cmd_encode_sweep)
    p = (
        sub.add_parser("representations", help="representation_status projection")
        .add_subparsers(dest="action", required=True)
        .add_parser("run", help="consume representation.ready / encode.failed")
    )
    p.add_argument("--exit-when-idle", type=int, default=None, help="stop after N empty polls")
    p.set_defaults(func=cmd_representations_run)
    p = sub.add_parser("rebuild", help="re-derive one media's read models")
    p.add_argument("media", help="media id (med_…) or locator URL")
    p.add_argument("--since", required=True, help="ISO time, e.g. 2026-09-01T00:00:00+00:00")
    p.add_argument("--until", default=None, help="ISO time (default: now)")
    p.set_defaults(func=cmd_rebuild)
    p = sub.add_parser("show", help="what the registry knows about one media")
    p.add_argument("media", help="media id (med_…) or locator URL")
    p.set_defaults(func=cmd_show)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings()
    configure_logging(settings)
    result: int = args.func(args, settings)
    return result


if __name__ == "__main__":
    sys.exit(main())
