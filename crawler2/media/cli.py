"""``crawler2-media``: the P8 media registry (docs/phases/p08-media-registry/).

``resolve run`` resolves ``media.discovered`` from the outbox (ADR-024). Run
it on one or more hosts: one holds the lease, the others stand by. Stopping
it never affects the crawl. ``resolve status`` exits 0 (ok, warning, never
started), 1 (lag beyond ``media.lag_critical_s``) or 2 (halted in a
retention gap). ``resolve accept-gap`` records a halted gap as accepted loss
and resumes at the oldest retained outbox bucket.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
from dataclasses import asdict
from datetime import timedelta
from typing import Any

from antipiracy_contracts.ownership import Component, Producer

from crawler2.core.configuration import Settings, WorkerRole
from crawler2.core.identity import WorkerIdentity
from crawler2.core.observability import Metrics, configure_logging
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

    def resolver(self, *, schedule: bool = True) -> Resolver:
        sink = None
        if schedule:
            sink = ProbeScheduler(
                self.work,
                reprobe_after=timedelta(seconds=self.settings.media.reprobe_after_s),
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings()
    configure_logging(settings)
    result: int = args.func(args, settings)
    return result


if __name__ == "__main__":
    sys.exit(main())
