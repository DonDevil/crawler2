# ADR-004 — Redis Streams as event transport, with an outbox

Status: Accepted (P0, 2026-09-28; outbox implemented by ADR-013)

## Context

Components react to each other through events (page stored, media found,
match result, …), within crawler2 and across to the fingerprinter, which
already consumes Redis Streams. Redis is already the coordination store
(frontier, leases, time — ADR-006).

## Decision

- **Redis Streams** with consumer groups are the event transport. Event
  payloads are `antipiracy-contracts` types (ADR-007); each event carries
  type, schema version, event id, producer worker identity and
  correlation id.
- Delivery is **at-least-once**. Every consumer is idempotent and safe
  under concurrent delivery on another host (plan B.4 #5); pending
  entries are reclaimed with `XAUTOCLAIM` after a lease-like idle time.
- Streams are bounded (`MAXLEN ~`/`MINID`); Redis is not the system of
  record for events.

## The dual-write problem

"Write entity to Scylla" and "publish event to Redis" are two systems; a
crash between them either loses the event (entity exists, nobody reacts)
or publishes an event for an entity that was never written.

Resolution (implemented in P2, not P0): **transactional outbox in
Scylla.** The entity write and an outbox row are written together (same
partition/batch where the data model allows; otherwise the outbox row is
written first and the relay tolerates missing entities). A relay publishes
outbox rows to Streams and marks them sent; because delivery is
at-least-once and consumers are idempotent, relay crashes cause only
duplicates, never loss. Event ownership (which service may emit which
event type) is defined in P1.

## Consequences

- P0 implements no event bus; it records the rule so that P1 contracts
  and P2 storage are designed for it.
- Redis durability (AOF `appendonly yes` in dev) protects in-flight
  coordination, but the outbox is what guarantees events.
- If throughput or retention outgrow Redis Streams, the transport can be
  replaced behind the same publisher/consumer interfaces (e.g. Kafka/
  Redpanda) without touching producers' domain logic.
