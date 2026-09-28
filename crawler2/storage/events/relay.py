"""Outbox relay: publish committed outbox rows, then mark them published (ADR-013).

Guarantee: every outbox row that is durable is published at least once,
provided the relay runs within the outbox retention (14 days). A crash at
any point causes re-publication (duplicates), never loss:

- crash before ``publish``      → rows stay unpublished, next cycle publishes
- crash after publish, before mark → next cycle publishes them again
- crash after mark               → nothing left to do

Scan strategy per shard (bounded cost, no unbounded partitions):

- every cycle: the current and previous minute bucket ("hot" buckets);
- every ``recheck_interval_s``: every bucket from the checkpoint to now; the
  checkpoint then advances over buckets older than ``settle_s`` (by then no
  in-flight write can still land in them);
- ``sweep``: an explicit rescan behind the checkpoint, the safety net for a
  row that became durable later than ``settle_s`` after its bucket (such
  rows are counted as ``late``; a non-zero count means settle_s is too small).

Several relays may run (one per host): they may publish the same row twice,
which the at-least-once contract already allows. Nothing here is exactly-once.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from prometheus_client import Counter

from crawler2.core.observability import Metrics, get_logger
from crawler2.storage.events.publisher import EventPublisher, OutboundEvent
from crawler2.storage.layout import OUTBOX_BUCKET_SECONDS, OUTBOX_SHARDS, outbox_bucket


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    shard: int
    bucket: int
    event_id: str
    event_type: str
    schema_version: str
    idempotency_key: str
    envelope: bytes
    enqueued_at: datetime
    published_at: datetime | None

    @property
    def outbound(self) -> OutboundEvent:
        return OutboundEvent(
            event_id=self.event_id,
            event_type=self.event_type,
            schema_major=int(self.schema_version.partition(".")[0]),
            envelope=self.envelope,
        )


class OutboxStore(Protocol):
    def rows(self, shard: int, bucket: int) -> list[OutboxRecord]: ...

    def mark_published(self, records: Sequence[OutboxRecord], at: datetime) -> None: ...

    def checkpoint(self, shard: int) -> int | None: ...

    def set_checkpoint(self, shard: int, next_bucket: int, *, by: str) -> None: ...

    def earliest_bucket(self) -> int:
        """First bucket that can hold rows (the outbox table's creation)."""
        ...


@dataclass(slots=True)
class RelayReport:
    published: int = 0
    late: int = 0
    buckets_scanned: int = 0
    checkpoints: dict[int, int] = field(default_factory=dict)


class OutboxRelay:
    def __init__(
        self,
        store: OutboxStore,
        publisher: EventPublisher,
        *,
        relay_id: str,
        settle_s: float,
        recheck_interval_s: float = 60.0,
        batch_size: int = 500,
        shards: Iterable[int] = range(OUTBOX_SHARDS),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        metrics: Metrics | None = None,
    ) -> None:
        self._store = store
        self._publisher = publisher
        self._relay_id = relay_id
        self._settle = timedelta(seconds=settle_s)
        self._recheck = timedelta(seconds=recheck_interval_s)
        self._batch_size = batch_size
        self._shards = tuple(shards)
        self._clock = clock
        self._last_full_scan: datetime | None = None
        self._checkpoints: dict[int, int] = {}  # re-read from the store on full scans
        self._log = get_logger("storage.outbox_relay")
        self._published: Counter | None = None
        self._late: Counter | None = None
        self._failures: Counter | None = None
        if metrics is not None:
            self._published = metrics.counter(
                "outbox_published_total", "Outbox rows handed to the transport", ["event_type"]
            )
            self._late = metrics.counter(
                "outbox_late_rows_total",
                "Unpublished rows found behind the checkpoint (settle window too small)",
            )
            self._failures = metrics.counter(
                "outbox_publish_failures_total", "Relay cycles aborted by a transport error"
            )

    # --- one cycle --------------------------------------------------------------

    def run_once(self) -> RelayReport:
        now = self._clock()
        report = RelayReport()
        full = self._last_full_scan is None or now - self._last_full_scan >= self._recheck
        current = outbox_bucket(now)
        settled_below = outbox_bucket(now - self._settle)  # buckets < this are settled
        try:
            for shard in self._shards:
                cached = None if full else self._checkpoints.get(shard)
                checkpoint = cached if cached is not None else self._store.checkpoint(shard)
                if checkpoint is None:
                    checkpoint = self._store.earliest_bucket()
                start = checkpoint if full else max(checkpoint, current - 1)
                next_checkpoint = checkpoint
                for bucket in range(start, current + 1):
                    report.published += self._publish_bucket(shard, bucket)
                    report.buckets_scanned += 1
                    if full and bucket == next_checkpoint and bucket < settled_below:
                        next_checkpoint = bucket + 1
                if next_checkpoint != checkpoint:
                    self._store.set_checkpoint(shard, next_checkpoint, by=self._relay_id)
                report.checkpoints[shard] = self._checkpoints[shard] = next_checkpoint
        except Exception:
            if self._failures is not None:
                self._failures.inc()
            raise
        if full:
            self._last_full_scan = now
        return report

    def _publish_bucket(self, shard: int, bucket: int, *, republish: bool = False) -> int:
        rows = self._store.rows(shard, bucket)
        todo = rows if republish else [r for r in rows if r.published_at is None]
        for start in range(0, len(todo), self._batch_size):
            chunk = todo[start : start + self._batch_size]
            self._publisher.publish([r.outbound for r in chunk])
            self._store.mark_published(chunk, self._clock())
            if self._published is not None:
                for record in chunk:
                    self._published.labels(event_type=record.event_type).inc()
        return len(todo)

    # --- recovery tools -----------------------------------------------------------

    def sweep(self, *, since: datetime) -> RelayReport:
        """Publish unpublished rows behind the checkpoints, back to ``since``."""
        report = RelayReport()
        for shard in self._shards:
            checkpoint = self._store.checkpoint(shard)
            if checkpoint is None:
                continue
            for bucket in range(outbox_bucket(since), checkpoint):
                found = self._publish_bucket(shard, bucket)
                report.late += found
                report.published += found
                report.buckets_scanned += 1
        if report.late:
            self._log.warning("outbox_late_rows", late=report.late)
            if self._late is not None:
                self._late.inc(report.late)
        return report

    def replay(self, *, since: datetime, until: datetime) -> RelayReport:
        """Re-publish every row (published or not) enqueued in [since, until]."""
        report = RelayReport()
        for shard in self._shards:
            for bucket in range(outbox_bucket(since), outbox_bucket(until) + 1):
                report.published += self._publish_bucket(shard, bucket, republish=True)
                report.buckets_scanned += 1
        return report

    def run_forever(self, *, poll_interval_s: float, stop: Callable[[], bool]) -> None:
        # A cycle drains every unpublished row it finds, so it always pauses afterwards:
        # the hot scan costs 2 reads per shard and must not spin.
        while not stop():
            report = self.run_once()
            if report.published:
                self._log.info("outbox_relayed", published=report.published)
            time.sleep(poll_interval_s)


__all__ = [
    "OUTBOX_BUCKET_SECONDS",
    "OutboxRecord",
    "OutboxRelay",
    "OutboxStore",
    "RelayReport",
]
