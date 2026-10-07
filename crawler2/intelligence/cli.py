"""``crawler2-intel``: P7 crawl intelligence (design §9).

``sources run`` folds ``fetch.completed`` from the outbox into the source
profiles (ADR-023). Run **one** instance per deployment. Stopping it never
affects the crawl: admission falls back once the projection is stale.
``sources rebuild`` starts a new generation from the outbox's retained
facts. ``sources status`` and ``sources report`` are read-only.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from crawler2.core.configuration import Settings, WorkerRole
from crawler2.core.identity import WorkerIdentity
from crawler2.core.observability import Metrics, configure_logging
from crawler2.intelligence.projection import ReaderConfig, SourceProjector
from crawler2.intelligence.sources import DEFAULT_POLICY, NEGATIVE_STATES, advise
from crawler2.intelligence.store import ScyllaSourceProfileStore


def _open(settings: Settings, metrics: Metrics | None = None) -> tuple[Any, SourceProjector]:
    from crawler2.storage.scylla import ScyllaStorage

    identity = str(WorkerIdentity.create(settings.host_id, WorkerRole.INTELLIGENCE))
    storage = ScyllaStorage.open(settings.scylla, instance=identity)
    intel = settings.intelligence
    config = ReaderConfig(
        projection=intel.projection,
        settle=timedelta(seconds=settings.events.relay_settle_s),
        retention=timedelta(seconds=intel.outbox_retention_s),
        retention_margin=timedelta(seconds=intel.retention_margin_s),
        max_buckets_per_cycle=intel.max_buckets_per_cycle,
        dedupe_window=intel.dedupe_window,
    )
    projector = SourceProjector(
        storage.outbox,
        ScyllaSourceProfileStore(storage.session),
        config=config,
        instance=identity,
        metrics=metrics,
    )
    return storage, projector


def cmd_run(args: Any, settings: Settings) -> int:
    metrics = Metrics(settings)
    metrics.serve()
    _, projector = _open(settings, metrics)
    stopping = False

    def stop(*_: object) -> None:
        nonlocal stopping
        stopping = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)
    projector.run_forever(
        poll_interval_s=settings.intelligence.poll_interval_s,
        retry_max_s=settings.intelligence.retry_max_s,
        stop=lambda: stopping,
    )
    return 0


def cmd_rebuild(args: Any, settings: Settings) -> int:
    _, projector = _open(settings)
    state = projector.rebuild()
    print(json.dumps({"rebuilt": state is not None} | _state_doc(state), default=str))
    return 0 if state is not None else 1


def _state_doc(state: Any) -> dict[str, Any]:
    if state is None:
        return {"state": None}
    now = datetime.now(UTC)
    return {"state": asdict(state), "lag_s": round(state.lag(now).total_seconds())}


def cmd_status(args: Any, settings: Settings) -> int:
    storage, _ = _open(settings)
    store = ScyllaSourceProfileStore(storage.session)
    state = store.state(settings.intelligence.projection)
    print(json.dumps(_state_doc(state), indent=2, default=str))
    return 0


def cmd_report(args: Any, settings: Settings) -> int:
    """Hosts in a negative state now, with the move target for the operator (D-15)."""
    storage, _ = _open(settings)
    store = ScyllaSourceProfileStore(storage.session)
    state = store.state(settings.intelligence.projection)
    now = datetime.now(UTC)
    rows = []
    for row in store.scan():
        if state is None or row.generation != state.generation:
            continue
        advice = advise(row.profile, now, DEFAULT_POLICY)
        if advice.state in NEGATIVE_STATES or args.all:
            rows.append(
                {
                    "domain_id": row.profile.domain_id,
                    "state": advice.state.value,
                    "admit": advice.admit,
                    "capability": advice.capability,
                    "moved_to": row.profile.moved_to,
                    "reason": advice.reason,
                }
            )
    for doc in sorted(rows, key=lambda d: (d["state"], d["domain_id"])):
        print(json.dumps(doc, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crawler2-intel", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sources = sub.add_parser("sources", help="source-health projection").add_subparsers(
        dest="action", required=True
    )
    sources.add_parser("run", help="fold the outbox into source profiles").set_defaults(
        func=cmd_run
    )
    sources.add_parser("rebuild", help="new generation from retained facts").set_defaults(
        func=cmd_rebuild
    )
    sources.add_parser("status", help="checkpoint, generation and lag").set_defaults(
        func=cmd_status
    )
    p = sources.add_parser("report", help="hosts in negative states (moved targets)")
    p.add_argument("--all", action="store_true", help="every host, not only negative ones")
    p.set_defaults(func=cmd_report)
    args = parser.parse_args(argv)
    settings = Settings()
    configure_logging(settings)
    result: int = args.func(args, settings)
    return result


if __name__ == "__main__":
    sys.exit(main())
