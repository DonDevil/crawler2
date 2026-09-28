# ADR-013 — Outbox, relay and consumer idempotency

Status: Accepted (P2, 2026-09-28). Implements the outbox of ADR-004.

## Context

A repository write and the event announcing it go to two systems
(Scylla, Redis Streams). Scylla has no multi-table ACID transaction.
P1 fixed the envelope, the JSON wire form and per-event idempotency keys,
and requires at-least-once delivery with consumer deduplication.

## Decision

**Outbox table** `outbox`, partition `(shard, bucket)`: shard =
`event_id % 32`, bucket = enqueue minute. Each row stores the full P1
envelope JSON (exactly the bytes later published), event type/version,
producer component, occurred_at, correlation/causation ids, the catalog
idempotency key, `enqueued_at` and `published_at`. TTL 14 days, TWCS.

**Atomicity, as Scylla actually provides it:**

- *Pattern A* — authoritative rows + outbox row in one **logged batch**.
  The batchlog guarantees that once the coordinator accepted the batch, all
  of its mutations are eventually applied, even if the coordinator dies; a
  client timeout means "unknown", and the caller retries the idempotent
  write. Logged batches give atomicity (all or nothing, eventually), **not
  isolation**: a reader can briefly see the entity before the outbox row.
  Used for fetch attempts, page observations, media observations.
- *Pattern B* — authoritative rows first, outbox row last, when the rows
  cannot share a batch: link batches (up to 10 000 rows exceed batch
  limits) and LWT evidence writes (conditional statements cannot join a
  multi-partition batch). A crash before the outbox row loses nothing
  because the producer is itself driven by an at-least-once event whose
  redelivery repeats the idempotent write and the outbox append.

**Relay** (`storage/events/relay.py`, `crawler2-storage relay`): reads
unpublished rows, publishes the stored bytes to Redis Streams
(`<prefix><event_type>.v<major>`, approximate MAXLEN), then marks
`published_at`. Every cycle scans the current and previous minute; every
`recheck_interval` it scans from a per-shard checkpoint and advances the
checkpoint over buckets older than `settle_s` (10 min). `sweep` rescans
behind the checkpoint and counts "late" rows; `replay` re-publishes a time
range. Several relays may run; they can only cause duplicates.

**Guarantees** (and nothing stronger):

| Stage | Guarantee |
|---|---|
| authoritative write | durable once acknowledged at LOCAL_QUORUM |
| outbox row | pattern A: in the same logged batch; pattern B: after the rows, completed by upstream redelivery |
| publication | at-least-once while the relay runs within the 14-day outbox retention; a relay crash causes re-publication, never loss |
| delivery | Redis Streams consumer groups; unacknowledged entries are reclaimed (XAUTOCLAIM) |
| consumer | `IdempotentConsumer`: decode via contracts → catalog idempotency key → skip if processed → apply → mark |

There is **no exactly-once** anywhere. Consumers must remain correct when
the same fact arrives twice, including with a *different* event id (a
retried producer allocates a new one) — hence keys from the catalog, not
event ids.

**Processed-event markers in Scylla, not Redis** (`processed_events`,
partition `(consumer, idempotency_key)`, TTL 14 days = the outbox
redelivery horizon): they must survive a Redis flush or failover, and Redis
memory is reserved for the frontier. Markers are an optimization and a
guard for side effects; correctness comes from idempotent repositories.

## Consequences

- A relay outage longer than 14 days would expire unpublished rows; the
  `outbox_late_rows_total` metric and pending age need alerting (P14).
- Outbox partitions are bounded by the cluster event rate: ≤ ~30k events/s
  keeps a (shard, minute) partition under 100 MB (benchmarks.md). Beyond
  that, raise the shard count (relays scan the union of old and new shards).
- The transport is replaceable behind `EventPublisher`; the stream name
  (`events:<type>.v<major>`) is the contract with the fingerprinter.
