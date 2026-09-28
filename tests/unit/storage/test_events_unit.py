"""Relay/consumer logic with in-memory stand-ins (the Scylla/Redis paths are integration tests)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from antipiracy_contracts.compat import event_fixtures
from antipiracy_contracts.events import (
    UnexpectedEventTypeError,
    decode_event,
    encode_event,
)
from antipiracy_contracts.events.fingerprinting import EncodeRequested
from antipiracy_contracts.events.web import PageObserved
from antipiracy_contracts.ids import EventId
from antipiracy_contracts.ownership import Component

from crawler2.storage.events.consumer import ConsumeOutcome, IdempotentConsumer
from crawler2.storage.events.idempotency import idempotency_key
from crawler2.storage.events.publisher import OutboundEvent
from crawler2.storage.events.relay import OutboxRecord, OutboxRelay
from crawler2.storage.layout import outbox_bucket
from tests.fixtures.contracts import SPEC, T0, content_key, event_of, media, page_observation, url

# --- idempotency keys ------------------------------------------------------------


def test_every_catalog_event_has_a_deterministic_key() -> None:
    for fixture in event_fixtures():
        envelope = decode_event(fixture.raw)
        key = idempotency_key(envelope)
        assert key.startswith(f'["{envelope.event_type}",')
        assert key == idempotency_key(decode_event(fixture.raw))


def test_key_ignores_event_id_but_not_the_fact() -> None:
    observation = page_observation(url("a"))
    first = event_of(PageObserved(observation=observation), Component.CRAWLER_WORKER)
    retried = event_of(PageObserved(observation=observation), Component.CRAWLER_WORKER)
    assert first.event_id != retried.event_id
    assert idempotency_key(first) == idempotency_key(retried)
    other = event_of(PageObserved(observation=page_observation(url("b"))), Component.CRAWLER_WORKER)
    assert idempotency_key(other) != idempotency_key(first)


def test_optional_key_components_are_part_of_the_key() -> None:
    content = content_key(b"sample")
    default = EncodeRequested(content=content, source=media("v.mp4"))
    explicit = EncodeRequested(content=content, source=media("v.mp4"), required_spec=SPEC)
    keys = {idempotency_key(event_of(p, Component.MEDIA_REGISTRY)) for p in (default, explicit)}
    assert len(keys) == 2


# --- relay -----------------------------------------------------------------------


class MemoryOutbox:
    def __init__(self, earliest: int) -> None:
        self.buckets: dict[tuple[int, int], dict[str, OutboxRecord]] = {}
        self.checkpoints: dict[int, int] = {}
        self._earliest = earliest

    def add(self, shard: int, bucket: int, n: int) -> list[str]:
        ids = []
        for _ in range(n):
            ident = str(EventId.new())
            self.buckets.setdefault((shard, bucket), {})[ident] = OutboxRecord(
                shard, bucket, ident, "page.observed", "1.0", "k" + ident, b"{}", T0, None
            )
            ids.append(ident)
        return ids

    def rows(self, shard: int, bucket: int) -> list[OutboxRecord]:
        return list(self.buckets.get((shard, bucket), {}).values())

    def mark_published(self, records: Sequence[OutboxRecord], at: datetime) -> None:
        for r in records:
            self.buckets[(r.shard, r.bucket)][r.event_id] = replace(r, published_at=at)

    def checkpoint(self, shard: int) -> int | None:
        return self.checkpoints.get(shard)

    def set_checkpoint(self, shard: int, next_bucket: int, *, by: str) -> None:
        self.checkpoints[shard] = next_bucket

    def earliest_bucket(self) -> int:
        return self._earliest

    def unpublished(self) -> int:
        return sum(r.published_at is None for b in self.buckets.values() for r in b.values())


class CrashingPublisher:
    """Delivers, then 'crashes' after ``crash_after`` events (before the relay marks them)."""

    def __init__(self, crash_after: int | None = None) -> None:
        self.delivered: list[str] = []
        self.crash_after = crash_after

    def publish(self, events: Sequence[OutboundEvent]) -> None:
        for event in events:
            self.delivered.append(event.event_id)
            if self.crash_after is not None and len(self.delivered) >= self.crash_after:
                raise ConnectionError("simulated crash after delivery, before mark")


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _relay(store: MemoryOutbox, publisher: CrashingPublisher, clock: Clock) -> OutboxRelay:
    return OutboxRelay(
        store, publisher, relay_id="t", settle_s=600, batch_size=3, shards=[0, 1], clock=clock
    )


def test_crash_between_delivery_and_mark_duplicates_but_never_loses() -> None:
    clock = Clock(T0)
    now = outbox_bucket(T0)
    store = MemoryOutbox(earliest=now - 5)
    produced = store.add(0, now - 2, 5) + store.add(1, now, 4)
    crashing = CrashingPublisher(crash_after=4)
    with pytest.raises(ConnectionError):
        _relay(store, crashing, clock).run_once()
    assert store.unpublished() > 0

    healthy = CrashingPublisher()
    _relay(store, healthy, clock).run_once()  # a fresh process: no memory of the crash
    assert store.unpublished() == 0
    delivered = crashing.delivered + healthy.delivered
    assert set(delivered) == set(produced)  # nothing lost
    assert len(delivered) > len(produced)  # at-least-once: duplicates happened


def test_checkpoint_only_moves_past_settled_buckets() -> None:
    clock = Clock(T0)
    now = outbox_bucket(T0)
    store = MemoryOutbox(earliest=now - 30)
    store.add(0, now - 20, 2)
    relay = _relay(store, CrashingPublisher(), clock)
    relay.run_once()
    assert store.checkpoints[0] == now - 10  # settle_s = 600 s = 10 buckets
    assert store.checkpoints[1] == now - 10

    # a row lands in an unsettled bucket after the scan: the next full scan finds it
    late = store.add(0, now - 5, 1)
    clock.now = T0 + timedelta(seconds=61)
    publisher = CrashingPublisher()
    _relay(store, publisher, clock).run_once()
    assert publisher.delivered == late


def test_sweep_finds_rows_behind_the_checkpoint_and_replay_republishes() -> None:
    clock = Clock(T0)
    now = outbox_bucket(T0)
    store = MemoryOutbox(earliest=now - 30)
    relay = _relay(store, CrashingPublisher(), clock)
    relay.run_once()
    behind = store.add(1, now - 25, 2)  # became durable far too late (behind checkpoint)
    publisher = CrashingPublisher()
    swept = _relay(store, publisher, clock).sweep(since=T0 - timedelta(hours=1))
    assert swept.late == 2
    assert publisher.delivered == behind

    replayer = CrashingPublisher()
    report = _relay(store, replayer, clock).replay(since=T0 - timedelta(minutes=30), until=T0)
    assert report.published == 2
    assert replayer.delivered == behind


# --- consumer --------------------------------------------------------------------


class MemoryProcessed:
    def __init__(self) -> None:
        self.marks: dict[tuple[str, str], str] = {}

    def is_processed(self, consumer: str, key: str) -> bool:
        return (consumer, key) in self.marks

    def mark_processed(
        self, consumer: str, key: str, *, event_id: str, event_type: str, at: datetime
    ) -> None:
        self.marks[(consumer, key)] = event_id


def test_consumer_applies_each_logical_event_once() -> None:
    applied: list[str] = []
    consumer = IdempotentConsumer(
        "extraction",
        PageObserved,
        lambda e: applied.append(e.payload.observation.observation_id),
        MemoryProcessed(),
    )
    observation = page_observation(url("x"))
    first = encode_event(event_of(PageObserved(observation=observation), Component.CRAWLER_WORKER))
    retry = encode_event(event_of(PageObserved(observation=observation), Component.CRAWLER_WORKER))
    assert consumer.handle(first) is ConsumeOutcome.APPLIED
    assert consumer.handle(first) is ConsumeOutcome.DUPLICATE
    assert consumer.handle(retry) is ConsumeOutcome.DUPLICATE  # new event id, same fact
    assert applied == [observation.observation_id]


def test_failed_handler_is_not_marked_and_is_retried() -> None:
    calls = 0

    def flaky(_: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient")

    consumer = IdempotentConsumer("c", PageObserved, flaky, MemoryProcessed())
    wire = encode_event(
        event_of(PageObserved(observation=page_observation(url("y"))), Component.CRAWLER_WORKER)
    )
    with pytest.raises(RuntimeError):
        consumer.handle(wire)
    assert consumer.handle(wire) is ConsumeOutcome.APPLIED
    assert calls == 2


def test_consumer_validates_through_the_contracts() -> None:
    consumer = IdempotentConsumer("c", PageObserved, lambda _: None, MemoryProcessed())
    other = next(f for f in event_fixtures() if f.name.startswith("fetch.completed"))
    with pytest.raises(UnexpectedEventTypeError):
        consumer.handle(other.raw)
    with pytest.raises(ValueError, match="validation error"):
        consumer.handle(b'{"envelope_version": 1}')
