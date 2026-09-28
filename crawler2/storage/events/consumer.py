"""Idempotent event consumption: decode → dedupe on the event's key → apply → mark.

At-least-once delivery means every handler may see an event more than once
(redelivery, relay duplicates, a producer retry with a new event id). The
repositories are idempotent, so re-applying is always *correct*; the
processed-event marker makes it *cheap* and protects handlers with side
effects (e.g. emitting further events). The marker is written after the
handler succeeds: a crash in between re-applies once more, never skips.

Concurrent delivery of one event to two hosts may apply it twice; this is
the same case as redelivery and needs no lock.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

import redis
from antipiracy_contracts.events import EventEnvelope, EventPayload, decode_event_as
from prometheus_client import Counter

from crawler2.core.observability import Metrics
from crawler2.storage.errors import StorageUnavailableError
from crawler2.storage.events.idempotency import idempotency_key


class ProcessedEventStore(Protocol):
    def is_processed(self, consumer: str, key: str) -> bool: ...

    def mark_processed(
        self, consumer: str, key: str, *, event_id: str, event_type: str, at: datetime
    ) -> None: ...


class ConsumeOutcome(StrEnum):
    APPLIED = "applied"
    DUPLICATE = "duplicate"


class IdempotentConsumer[P: EventPayload]:
    def __init__(
        self,
        name: str,
        payload_type: type[P],
        handler: Callable[[EventEnvelope[P]], None],
        store: ProcessedEventStore,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        metrics: Metrics | None = None,
    ) -> None:
        self.name = name
        self._payload_type = payload_type
        self._handler = handler
        self._store = store
        self._clock = clock
        self._events: Counter | None = None
        if metrics is not None:
            self._events = metrics.counter(
                "consumer_events_total", "Events consumed, by outcome", ["consumer", "outcome"]
            )

    def handle(self, wire: bytes | str) -> ConsumeOutcome:
        """Raises the contract errors of ``decode_event_as`` for invalid input."""
        envelope = decode_event_as(wire, self._payload_type)
        key = idempotency_key(envelope)
        if self._store.is_processed(self.name, key):
            outcome = ConsumeOutcome.DUPLICATE
        else:
            self._handler(envelope)
            self._store.mark_processed(
                self.name,
                key,
                event_id=envelope.event_id,
                event_type=envelope.event_type,
                at=self._clock(),
            )
            outcome = ConsumeOutcome.APPLIED
        if self._events is not None:
            self._events.labels(consumer=self.name, outcome=outcome.value).inc()
        return outcome


@dataclass(frozen=True, slots=True)
class StreamEntry:
    entry_id: str
    envelope: bytes


class RedisStreamReader:
    """Minimal consumer-group reader for one stream (XREADGROUP / XACK / XAUTOCLAIM)."""

    def __init__(self, client: redis.Redis, stream: str, group: str, consumer: str) -> None:
        self._client = client
        self.stream = stream
        self.group = group
        self.consumer = consumer

    def ensure_group(self) -> None:
        try:
            self._client.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def read(self, *, count: int = 100, block_ms: int | None = None) -> list[StreamEntry]:
        try:
            reply: Any = self._client.xreadgroup(
                self.group, self.consumer, {self.stream: ">"}, count=count, block=block_ms
            )
        except redis.RedisError as exc:
            raise StorageUnavailableError(str(exc)) from exc
        return [self._entry(i, f) for _, entries in reply or [] for i, f in entries]

    def claim_stale(self, *, min_idle_ms: int, count: int = 100) -> list[StreamEntry]:
        """Take over entries another consumer read but never acknowledged (it crashed)."""
        reply: Any = self._client.xautoclaim(
            self.stream, self.group, self.consumer, min_idle_ms, "0-0", count=count
        )
        return [self._entry(i, f) for i, f in reply[1]]

    def ack(self, entries: list[StreamEntry]) -> None:
        if entries:
            self._client.xack(self.stream, self.group, *[e.entry_id for e in entries])

    @staticmethod
    def _entry(entry_id: bytes | str, fields: dict[bytes, bytes]) -> StreamEntry:
        ident = entry_id.decode() if isinstance(entry_id, bytes) else entry_id
        return StreamEntry(ident, fields[b"envelope"])
