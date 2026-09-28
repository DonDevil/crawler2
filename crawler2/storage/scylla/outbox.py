"""Scylla outbox (X1/X2) and processed-event markers (X3). See ADR-013."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from antipiracy_contracts.events import EventEnvelope, EventPayload, encode_event
from antipiracy_contracts.ids import EventId
from cassandra.query import BoundStatement

from crawler2.storage.events.idempotency import idempotency_key
from crawler2.storage.events.relay import OutboxRecord
from crawler2.storage.layout import OUTBOX_SHARDS, outbox_bucket, shard_of
from crawler2.storage.scylla.session import Consistency, ScyllaSession, utc

_INSERT = (
    "INSERT INTO {ks}.outbox (shard, bucket, event_id, event_type, schema_version, "
    "producer_component, occurred_at, correlation_id, causation_id, idempotency_key, "
    "envelope, enqueued_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_ROWS = (
    "SELECT shard, bucket, event_id, event_type, schema_version, idempotency_key, envelope, "
    "enqueued_at, published_at FROM {ks}.outbox WHERE shard = ? AND bucket = ?"
)
_MARK = "UPDATE {ks}.outbox SET published_at = ? WHERE shard = ? AND bucket = ? AND event_id = ?"
_CHECKPOINT = "SELECT next_bucket FROM {ks}.outbox_relay_checkpoints WHERE shard = ?"
_SET_CHECKPOINT = (
    "UPDATE {ks}.outbox_relay_checkpoints SET next_bucket = ?, updated_at = ?, updated_by = ? "
    "WHERE shard = ?"
)
_CREATED = "SELECT applied_at FROM {ks}.schema_migrations WHERE version = 1"
# Rows cannot predate the outbox table; the margin covers clock skew between hosts.
_EARLIEST_MARGIN = timedelta(hours=1)


class ScyllaOutbox:
    def __init__(self, session: ScyllaSession) -> None:
        self._s = session

    def statement[P: EventPayload](
        self, envelope: EventEnvelope[P], *, now: datetime | None = None
    ) -> BoundStatement:
        """The outbox row for ``envelope``; put it in the same logged batch as the
        authoritative rows it announces (ADR-013 pattern A)."""
        enqueued_at = now or datetime.now(UTC)
        return self._s.bind(
            _INSERT,
            Consistency.AUTHORITATIVE_WRITE,
            (
                shard_of(envelope.event_id, OUTBOX_SHARDS),
                outbox_bucket(enqueued_at),
                envelope.event_id.uuid,
                envelope.event_type,
                envelope.schema_version,
                envelope.producer.component.value,
                envelope.occurred_at,
                envelope.correlation_id.uuid if envelope.correlation_id else None,
                envelope.causation_id.uuid if envelope.causation_id else None,
                idempotency_key(envelope),
                encode_event(envelope).decode("utf-8"),
                enqueued_at,
            ),
        )

    def enqueue[P: EventPayload](self, envelope: EventEnvelope[P]) -> None:
        """Standalone append (pattern B: after the authoritative write is durable)."""
        self._s.execute(self.statement(envelope))

    # --- OutboxStore ------------------------------------------------------------

    def rows(self, shard: int, bucket: int) -> list[OutboxRecord]:
        rows = self._s.execute(self._s.bind(_ROWS, Consistency.STRONG_READ, (shard, bucket)))
        return [
            OutboxRecord(
                shard=r.shard,
                bucket=r.bucket,
                event_id=str(EventId.from_uuid(r.event_id)),
                event_type=r.event_type,
                schema_version=r.schema_version,
                idempotency_key=r.idempotency_key,
                envelope=r.envelope.encode("utf-8"),
                enqueued_at=utc(r.enqueued_at),
                published_at=utc(r.published_at) if r.published_at else None,
            )
            for r in rows
        ]

    def mark_published(self, records: Sequence[OutboxRecord], at: datetime) -> None:
        by_partition: dict[tuple[int, int], list[BoundStatement]] = {}
        for record in records:
            by_partition.setdefault((record.shard, record.bucket), []).append(
                self._s.bind(
                    _MARK,
                    Consistency.AUTHORITATIVE_WRITE,
                    (at, record.shard, record.bucket, EventId(record.event_id).uuid),
                )
            )
        self._s.execute_all(
            self._s.partition_batch(stmts, Consistency.AUTHORITATIVE_WRITE)
            for stmts in by_partition.values()
        )

    def checkpoint(self, shard: int) -> int | None:
        rows = self._s.execute(self._s.bind(_CHECKPOINT, Consistency.STRONG_READ, (shard,)))
        return rows[0].next_bucket if rows and rows[0].next_bucket is not None else None

    def set_checkpoint(self, shard: int, next_bucket: int, *, by: str) -> None:
        self._s.execute(
            self._s.bind(
                _SET_CHECKPOINT,
                Consistency.AUTHORITATIVE_WRITE,
                (next_bucket, datetime.now(UTC), by, shard),
            )
        )

    def earliest_bucket(self) -> int:
        rows = self._s.execute(self._s.bind(_CREATED, Consistency.STRONG_READ, ()))
        if not rows:
            return outbox_bucket(datetime.now(UTC))
        return outbox_bucket(utc(rows[0].applied_at) - _EARLIEST_MARGIN)


_SEEN = "SELECT event_id FROM {ks}.processed_events WHERE consumer = ? AND idempotency_key = ?"
_MARK_SEEN = (
    "INSERT INTO {ks}.processed_events (consumer, idempotency_key, event_id, event_type, "
    "processed_at) VALUES (?, ?, ?, ?, ?)"
)


class ScyllaProcessedEventStore:
    """X3 in Scylla, not Redis: markers must survive a Redis flush/failover and
    Redis memory is reserved for the frontier. TTL = outbox retention (14 d)."""

    def __init__(self, session: ScyllaSession) -> None:
        self._s = session

    def is_processed(self, consumer: str, key: str) -> bool:
        return bool(
            self._s.execute(self._s.bind(_SEEN, Consistency.EVENTUAL_READ, (consumer, key)))
        )

    def mark_processed(
        self, consumer: str, key: str, *, event_id: str, event_type: str, at: datetime
    ) -> None:
        self._s.execute(
            self._s.bind(
                _MARK_SEEN,
                Consistency.DERIVED_WRITE,
                (consumer, key, EventId(event_id).uuid, event_type, at),
            )
        )
