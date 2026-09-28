"""P2 latency benchmark: storage p99 at 10x the expected per-host load.

Expected per-host load (benchmarks.md): 10 fetches/s per host — 10x the V1
baseline of 1.02 pages/s, and what 64 HTTP slots sustain at a ~5 s median
fetch+politeness cycle. 10x that = 100 "fetch units"/s per host. Each unit
performs the storage work one fetch causes (P1 catalog frequencies):

    every unit   W5 latest read, 2x W12 url-state read, attempt record (+event),
                 32 KiB snapshot put (MinIO), observation record (+event),
                 4 newly discovered URLs, consumer dedupe check+mark
    50% units    link batch of 20 (+event; a new page version)
    30% units    media observation (+event), M5 content lookup, P1 status lookup
    10% units    standalone outbox append
     5% units    W7 strong read, object HEAD, object GET (verified)

The load is open-loop (units start on a fixed schedule, independent of how
long earlier units took); ``schedule_lag`` shows whether the client kept up.
A relay thread publishes the outbox to Redis meanwhile; publication lag is
measured from the outbox rows afterwards. Scylla and MinIO operations are
reported separately.

    PYTHONPATH=/app python benchmarks/p2-storage/latency.py [--rate 100] [--duration 60]
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import random
import resource
import statistics
import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import redis
from antipiracy_contracts.digests import ContentDigest
from antipiracy_contracts.events import EventEnvelope, new_event
from antipiracy_contracts.events.media import MediaObserved
from antipiracy_contracts.events.web import FetchCompleted, PageObserved, UrlsDiscovered
from antipiracy_contracts.ids import UrlId
from antipiracy_contracts.models.blobs import BlobRef
from antipiracy_contracts.models.media import MediaObservation
from antipiracy_contracts.models.web import FetchAttempt, PageObservation, UrlRef
from antipiracy_contracts.ownership import Component

from crawler2.core.configuration import Settings
from crawler2.storage.events.idempotency import idempotency_key
from crawler2.storage.events.publisher import RedisStreamPublisher
from crawler2.storage.events.relay import OutboxRelay
from crawler2.storage.objectstore import S3ObjectStore
from crawler2.storage.objectstore.layout import raw_key, uri_of
from crawler2.storage.scylla import Migrator, ScyllaSession, ScyllaStorage
from tests.fixtures.contracts import (
    content_key,
    fetch_attempt,
    links,
    media,
    media_observation,
    page_observation,
    producer,
    url,
)

EXPECTED_PER_HOST = 10
KNOWN_URLS = 5_000
SNAPSHOT_BYTES = 32 * 1024

TARGETS_MS = {
    # Scylla point operations
    "latest_read": 25,
    "url_state_read": 25,
    "observation_get": 25,
    "representation_lookup": 25,
    "dedupe_check": 25,
    "dedupe_mark": 25,
    "outbox_append": 25,
    # Scylla fan-out / multi-statement repository operations
    "content_lookup": 50,
    "discovered_record": 50,
    "attempt_record": 50,
    "observation_record": 100,
    "media_record": 100,
    "links_record": 150,
    # object store (MinIO), reported separately
    "object_put": 250,
    "object_head": 50,
    "object_get": 100,
}
PUBLICATION_LAG_TARGET_S = 5.0


@dataclass
class Unit:
    now: datetime
    reads: list[UrlId]
    attempt: FetchAttempt
    attempt_event: EventEnvelope[FetchCompleted]
    body: bytes
    ref: BlobRef
    observation: PageObservation
    observed: EventEnvelope[PageObserved]
    observed_key: str
    discovered_urls: list[UrlRef]
    links: UrlsDiscovered | None = None
    links_event: EventEnvelope[UrlsDiscovered] | None = None
    media: MediaObservation | None = None
    media_event: EventEnvelope[MediaObserved] | None = None
    extra_event: EventEnvelope[PageObserved] | None = None
    point_reads: bool = False


class Recorder:
    def __init__(self) -> None:
        self.samples: dict[str, list[float]] = defaultdict(list)
        self.errors: dict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    @contextmanager
    def op(self, name: str) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        except Exception:
            with self._lock:
                self.errors[name] += 1
            raise
        elapsed = (time.perf_counter() - started) * 1000
        with self._lock:
            self.samples[name].append(elapsed)


def _pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def _summary(values: list[float]) -> dict[str, float]:
    return {
        "count": len(values),
        "p50_ms": round(_pct(values, 0.50), 2),
        "p95_ms": round(_pct(values, 0.95), 2),
        "p99_ms": round(_pct(values, 0.99), 2),
        "max_ms": round(max(values), 2),
        "mean_ms": round(statistics.fmean(values), 2),
    }


def prepare(settings: Settings, keyspace: str) -> None:
    with ScyllaSession.connect(settings.scylla, keyspace=keyspace) as session:
        session.execute_raw(f"DROP KEYSPACE IF EXISTS {keyspace}")
        Migrator(session, settings.scylla, applied_by=f"{settings.host_id}:bench").migrate()
        storage = ScyllaStorage.over(session)
        known = [url(f"known/{i}", f"k{i % 50}.example") for i in range(KNOWN_URLS)]
        with ThreadPoolExecutor(24) as pool:
            list(pool.map(lambda u: storage.pages.record(page_observation(u)), known))


def run(args: argparse.Namespace, worker_no: int) -> dict[str, Any]:
    """One load process: ``rate / processes`` units/s; process 0 also runs the relay."""
    settings = Settings()
    host = settings.host_id
    rate = args.rate / args.processes
    storage = ScyllaStorage.open(settings.scylla, keyspace=args.keyspace, instance=host)
    scratch = Path(settings.scratch_dir)
    store = S3ObjectStore(settings.minio, scratch_dir=scratch, bucket="crawler2-bench")
    store.ensure_bucket()
    client = redis.Redis(host=settings.redis.host, port=settings.redis.port)
    events_cfg = settings.events.model_copy(update={"stream_prefix": f"bench-{host}:"})
    worker = producer(Component.CRAWLER_WORKER, f"{host}:http:{os.getpid()}:{'0' * 32}")
    extractor = producer(Component.EXTRACTION, worker.instance)
    registry = producer(Component.MEDIA_REGISTRY, worker.instance)
    known = [url(f"known/{i}", f"k{i % 50}.example") for i in range(KNOWN_URLS)]
    rec = Recorder()
    lags: list[float] = []
    stop = threading.Event()

    relay = OutboxRelay(
        storage.outbox,
        RedisStreamPublisher(client, events_cfg),
        relay_id=f"{host}:bench",
        settle_s=600,
        recheck_interval_s=30,
    )

    def relay_loop() -> None:
        while not stop.is_set():
            relay.run_once()
            stop.wait(settings.events.relay_poll_interval_s)

    def build(n: int) -> Unit:
        """All contract objects and bytes of one unit, built *before* the timed phase."""
        n = n * args.processes + worker_no  # unique across processes
        rng = random.Random(f"{host}-{n}")  # noqa: S311 — load mix, not security
        now = datetime.now(UTC) - timedelta(seconds=1)
        page = url(f"u/{host}/{n}", f"site{n % 200}.example")
        attempt = fetch_attempt(page, at=now, seed=f"{host}-a{n}")
        body = os.urandom(SNAPSHOT_BYTES)
        digest = ContentDigest.of_bytes(body)
        ref = BlobRef(
            uri=uri_of(store.bucket, raw_key(digest)),
            digest=digest,
            size_bytes=len(body),
            media_type="text/html",
        )
        observation = page_observation(page, at=now, body=body, snapshot=ref, seed=f"{host}-o{n}")
        observed = new_event(
            PageObserved(observation=observation), producer=worker, occurred_at=now
        )
        unit = Unit(
            now=now,
            reads=[rng.choice(known).url_id for _ in range(3)],
            attempt=attempt,
            attempt_event=new_event(
                FetchCompleted(attempt=attempt), producer=worker, occurred_at=now
            ),
            body=body,
            ref=ref,
            observation=observation,
            observed=observed,
            observed_key=idempotency_key(observed),
            discovered_urls=[url(f"new/{host}/{n}/{i}", page.url.host) for i in range(4)],
        )
        if rng.random() < 0.5:
            unit.links = UrlsDiscovered(
                page_observation_id=observation.observation_id,
                page=page,
                page_version_id=observation.page_version_id,
                links=links(20, host=page.url.host, prefix=f"l{n}"),
            )
            unit.links_event = new_event(unit.links, producer=extractor, occurred_at=now)
        if rng.random() < 0.3:
            content = content_key(f"{host}-{n % 1000}".encode())
            unit.media = media_observation(
                media(f"{host}/{n % 1000}.mp4"), observation, content=content
            )
            unit.media_event = new_event(
                MediaObserved(observation=unit.media), producer=registry, occurred_at=now
            )
        if rng.random() < 0.1:
            unit.extra_event = new_event(
                PageObserved(observation=observation), producer=worker, occurred_at=now
            )
        unit.point_reads = rng.random() < 0.05
        return unit

    def execute(u: Unit, intended: float) -> None:
        lags.append((time.perf_counter() - intended) * 1000)
        with rec.op("latest_read"):
            storage.pages.latest(u.reads[0])
        for url_id in u.reads[1:]:
            with rec.op("url_state_read"):
                storage.urls.get(url_id)
        with rec.op("attempt_record"):
            storage.fetch_attempts.record(u.attempt, event=u.attempt_event)
        with rec.op("object_put"):
            store.put(u.body, media_type="text/html", expected_digest=u.ref.digest)
        with rec.op("observation_record"):
            storage.pages.record(u.observation, event=u.observed)
        with rec.op("discovered_record"):
            storage.urls.record_discovered(u.discovered_urls, seen_at=u.now)
        with rec.op("dedupe_check"):
            storage.processed_events.is_processed("bench-extraction", u.observed_key)
        with rec.op("dedupe_mark"):
            storage.processed_events.mark_processed(
                "bench-extraction",
                u.observed_key,
                event_id=u.observed.event_id,
                event_type=u.observed.event_type,
                at=u.now,
            )
        if u.links is not None:
            with rec.op("links_record"):
                storage.links.record(u.links, observed_at=u.now, event=u.links_event)
        if u.media is not None:
            content_id = u.media.content.content_id if u.media.content else None
            with rec.op("media_record"):
                storage.media.record_observation(u.media, event=u.media_event)
            if content_id is not None:
                with rec.op("content_lookup"):
                    storage.media.by_content(content_id)
                with rec.op("representation_lookup"):
                    storage.projections.representation_status(content_id)
        if u.extra_event is not None:
            with rec.op("outbox_append"):
                storage.outbox.enqueue(u.extra_event)
        if u.point_reads:
            with rec.op("observation_get"):
                storage.pages.get(u.observation.observation_id)
            with rec.op("object_head"):
                store.stat(u.ref.digest)
            with rec.op("object_get"):
                store.get(u.ref)

    relay_thread = threading.Thread(target=relay_loop, daemon=True)
    if worker_no == 0:
        relay_thread.start()
    total = int(rate * args.duration)
    units = [build(n) for n in range(total)]  # untimed: contract construction is not storage
    cpu0 = resource.getrusage(resource.RUSAGE_SELF)
    started = time.perf_counter()
    failures = 0
    with ThreadPoolExecutor(args.threads) as pool:
        futures = []
        for n in range(total):
            intended = started + n / rate
            delay = intended - time.perf_counter()
            if delay > 0:
                time.sleep(delay)  # pacing the open-loop schedule, not synchronization
            futures.append(pool.submit(execute, units[n], intended))
        for future in futures:
            if future.exception() is not None:
                failures += 1
    wall = time.perf_counter() - started
    cpu1 = resource.getrusage(resource.RUSAGE_SELF)
    pub_lag: list[float] = []
    if worker_no == 0:
        time.sleep(5)  # let the relay drain the tail before measuring publication lag
        stop.set()
        relay_thread.join(timeout=30)
        relay.run_once()
        pub_lag = [
            (r.published_at - r.enqueued_at).total_seconds()
            for r in storage.session.execute_raw(
                f"SELECT enqueued_at, published_at FROM {args.keyspace}.outbox"
            )
            if r.published_at is not None
        ]
    store.close()
    storage.close()
    return {
        "host": host,
        "units": total,
        "failed_units": failures,
        "wall_s": wall,
        "cpu_s": (cpu1.ru_utime + cpu1.ru_stime) - (cpu0.ru_utime + cpu0.ru_stime),
        "lags": lags,
        "samples": dict(rec.samples),
        "errors": dict(rec.errors),
        "pub_lag": pub_lag,
    }


def merge(args: argparse.Namespace, parts: list[dict[str, Any]]) -> dict[str, Any]:
    samples: dict[str, list[float]] = defaultdict(list)
    errors: dict[str, int] = defaultdict(int)
    for part in parts:
        for name, values in part["samples"].items():
            samples[name] += values
        for name, count in part["errors"].items():
            errors[name] += count
    units = sum(p["units"] for p in parts)
    wall = max(p["wall_s"] for p in parts)
    pub_lag = [lag for p in parts for lag in p["pub_lag"]]
    return {
        "host": parts[0]["host"],
        "processes": args.processes,
        "rate_target_units_per_s": args.rate,
        "expected_per_host_units_per_s": EXPECTED_PER_HOST,
        "duration_s": args.duration,
        "units": units,
        "failed_units": sum(p["failed_units"] for p in parts),
        "achieved_units_per_s": round(units / wall, 1),
        "client_cpu_utilisation": round(sum(p["cpu_s"] for p in parts) / wall, 2),
        "schedule_lag": _summary([lag for p in parts for lag in p["lags"]]),
        "publication_lag_s": {
            "count": len(pub_lag),
            "p50": round(_pct(pub_lag, 0.5), 2) if pub_lag else None,
            "p99": round(_pct(pub_lag, 0.99), 2) if pub_lag else None,
        },
        "operations": {name: _summary(v) for name, v in sorted(samples.items())},
        "errors": dict(errors),
        "targets_ms": TARGETS_MS,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keyspace", default="crawler2_bench")
    parser.add_argument("--rate", type=float, default=10 * EXPECTED_PER_HOST)
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--threads", type=int, default=32)
    parser.add_argument("--processes", type=int, default=1)
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    if args.reset:
        prepare(settings, args.keyspace)
    if args.prepare_only:
        return
    with multiprocessing.get_context("spawn").Pool(args.processes) as pool:
        parts = pool.starmap(run, [(args, i) for i in range(args.processes)])
    client = redis.Redis(host=settings.redis.host, port=settings.redis.port)
    for key in client.scan_iter(match=f"bench-{settings.host_id}:*"):
        client.delete(key)
    print(json.dumps(merge(args, parts), indent=2))


if __name__ == "__main__":
    main()
