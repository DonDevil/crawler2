"""``crawler2-intel``: P7 crawl intelligence (design §9).

``sources run`` folds ``fetch.completed`` from the outbox into the source
profiles (ADR-023). Run **one** instance per deployment. Stopping it never
affects the crawl: admission falls back once the projection is stale.
``sources rebuild`` starts a new generation from the outbox's retained
facts. ``sources status`` and ``sources report`` are read-only.

``feedback run`` (P11) consumes ``match.found`` and ``target.registered`` /
``target.retired`` into match evidence and the target projection; any number
of instances may run (consumer group, idempotent writes). ``feedback show``
prints a host's assessment and a URL's effective priority; ``feedback
rebuild`` re-derives all evidence from the match projections.
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
    metrics.serve("intel")
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


def _feedback(settings: Settings, metrics: Metrics | None = None) -> tuple[Any, Any, Any]:
    from crawler2.intelligence.feedback_service import FeedbackMetrics, MatchFeedback
    from crawler2.intelligence.feedback_store import ScyllaFeedbackStore
    from crawler2.storage.scylla import ScyllaStorage

    identity = str(WorkerIdentity.create(settings.host_id, WorkerRole.INTELLIGENCE))
    storage = ScyllaStorage.open(settings.scylla, instance=identity)
    store = ScyllaFeedbackStore(storage.session)
    feedback = MatchFeedback(
        media=storage.media,
        pages=storage.pages,
        projections=storage.projections,
        store=store,
        metrics=FeedbackMetrics(metrics),
    )
    return storage, store, feedback


def cmd_feedback_run(args: Any, settings: Settings) -> int:
    from antipiracy_contracts.events.fingerprinting import MatchFound
    from antipiracy_contracts.events.targets import TargetRegistered, TargetRetired

    from crawler2.intelligence.feedback_service import (
        FeedbackLoop,
        FeedbackMetrics,
        TargetFeedback,
    )
    from crawler2.storage.events.consumer import (
        IdempotentConsumer,
        RedisStreamReader,
        connect_stream_client,
    )
    from crawler2.storage.events.publisher import stream_name

    metrics = Metrics(settings)
    metrics.serve("feedback")
    storage, _, feedback = _feedback(settings, metrics)
    targets = TargetFeedback(storage.projections)
    fb = settings.feedback
    client = connect_stream_client(settings.redis)
    identity = str(WorkerIdentity.create(settings.host_id, WorkerRole.INTELLIGENCE))
    processed = storage.processed_events
    pairs: list[Any] = []
    handlers: tuple[tuple[Any, Any], ...] = (
        (TargetRegistered, targets.registered),
        (TargetRetired, targets.retired),
        (MatchFound, feedback.handle),
    )
    for payload, handler in handlers:
        reader = RedisStreamReader(
            client,
            stream_name(settings.events, payload.EVENT_TYPE, payload.SCHEMA_MAJOR),
            fb.consumer_group,
            identity,
        )
        reader.ensure_group()
        consumer = IdempotentConsumer(
            fb.consumer_group, payload, handler, processed, metrics=metrics
        )
        pairs.append((reader, consumer))
    loop = FeedbackLoop(
        pairs,
        batch_size=fb.batch_size,
        block_ms=fb.block_ms,
        claim_idle_ms=fb.claim_idle_ms,
        metrics=FeedbackMetrics(metrics),
    )
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: loop.stop())
    loop.run(max_idle_polls=args.exit_when_idle)
    return 0


def cmd_feedback_rebuild(args: Any, settings: Settings) -> int:
    from crawler2.intelligence.feedback_service import rebuild

    storage, store, feedback = _feedback(settings)
    report = rebuild(
        feedback, store, storage.projections, now=datetime.now(UTC), months=args.months
    )
    print(json.dumps(report, sort_keys=True))
    return 0


def cmd_feedback_show(args: Any, settings: Settings) -> int:
    """Every host with evidence (or one host / URL): value, boost, patterns, capability."""
    from antipiracy_contracts.models.web import UrlRef

    from crawler2.intelligence.feedback_service import build_prioritizer

    storage, _, _ = _feedback(settings)
    prioritizer = build_prioritizer(settings, storage, None)
    if not prioritizer.refresh():
        print(json.dumps({"error": "snapshot unavailable"}))
        return 1
    now = datetime.now(UTC)
    url = UrlRef.of(args.url) if args.url else None
    snapshot = prioritizer.snapshot
    assert snapshot is not None  # noqa: S101 -- refreshed above
    domains = [str(url.domain_id)] if url else sorted(snapshot.evidence)
    for domain in domains:
        s = prioritizer.source(domain, now)
        doc: dict[str, Any] = {"domain_id": domain, "evidence": s is not None}
        if s is not None:
            doc |= {
                "value": round(s.value, 4),
                "boost": s.boost,
                "patterns": dict(s.patterns),
                "capability_weight": {k: round(v, 4) for k, v in s.capability_weight.items()},
                "targets": s.targets,
                "matches": s.matches,
                "last_match_at": s.last_match_at,
            }
        if url is not None:
            d = prioritizer.prioritize(url, args.base, now)
            doc["effective"] = asdict(d)
        print(json.dumps(doc, sort_keys=True, default=str))
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
    feedback = sub.add_parser("feedback", help="P11 match feedback").add_subparsers(
        dest="action", required=True
    )
    p = feedback.add_parser("run", help="consume match.found / target events into evidence")
    p.add_argument("--exit-when-idle", type=int, default=None, help="stop after N empty polls")
    p.set_defaults(func=cmd_feedback_run)
    p = feedback.add_parser("rebuild", help="drop and re-derive evidence from match projections")
    p.add_argument("--months", type=int, default=3, help="months of matches to replay")
    p.set_defaults(func=cmd_feedback_rebuild)
    p = feedback.add_parser("show", help="host assessments and a URL's effective priority")
    p.add_argument("--url", default=None, help="one URL: its host and effective priority")
    p.add_argument("--base", type=int, default=50, help="static priority to boost (P6 link=50)")
    p.set_defaults(func=cmd_feedback_show)
    args = parser.parse_args(argv)
    settings = Settings()
    configure_logging(settings)
    result: int = args.func(args, settings)
    return result


if __name__ == "__main__":
    sys.exit(main())
