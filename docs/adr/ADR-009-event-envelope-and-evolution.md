# ADR-009 — Event envelope, ownership catalog and contract evolution

Status: Accepted (P1, 2026-09-28)

## Context

ADR-004 fixed Redis Streams + outbox as the transport, with at-least-once
delivery. V1's crawler and fingerprinter drifted: the fingerprinter's job
schema gained a `schema_version` only late, and priority was encoded as
*which stream* a job went to (D12). V2 components run as separate processes
on several hosts, so every event is a wire contract, including events that
stay inside crawler2.

## Decision

### Envelope (`antipiracy_contracts.events.envelope`)

| Field | Required | Meaning |
|---|---|---|
| `envelope_version` | yes | `1`. Envelope layout version, independent of payload versions |
| `event_id` | yes | `EventId` (UUIDv7). Unchanged across redelivery |
| `event_type` | yes | `<noun>.<past-tense verb>`, e.g. `media.discovered` |
| `schema_version` | yes | `"MAJOR.MINOR"` of the payload schema |
| `occurred_at` | yes | UTC instant the described occurrence happened (not the publish time) |
| `producer` | yes | `{service, component, instance}`. `component` must own `event_type` |
| `correlation_id` | no | shared by all events in one causal chain |
| `causation_id` | no | `event_id` of the event that directly caused this one |
| `metadata` | no (`{}`) | ≤32 string pairs; diagnostic extension boundary only |
| `payload` | yes | the typed payload for (`event_type`, major) |

The envelope carries no transport concepts (stream names, partitions,
acknowledgement, retry counts). Publish time, stream IDs and delivery
counts belong to the transport layer (P2).

### Ownership catalog (`antipiracy_contracts.events.catalog`)

Every (event type, major) has exactly **one producing component**.
Consumers subscribe but never co-own the schema. The catalog also records
consumers, meaning, idempotency key and ordering assumptions.
`new_event` refuses to wrap a payload for a non-owner, and `decode_event`
rejects an event whose producer is not the owner (tested). The catalog
validates itself at import time: unique entries, at least one consumer,
producer not among its own consumers, and idempotency-key paths that
exist.

### Delivery semantics (all events)

At-least-once, duplicates allowed, no cross-event ordering. Each consumer
deduplicates on the event's **idempotency key**, which is a set of payload
fields (not `event_id`: two producers' retries of the same logical
occurrence carry different event IDs but the same key). Where "latest"
matters, consumers compare timestamps or versions in the payload, never
arrival order.

### Evolution policy

- **Stable semantics.** A field's meaning never changes within a major.
- **Minor (additive, backward compatible):** add an *optional* field (with
  a default), add a new event type, widen a constraint, add a consumer.
  Increment the catalog `minor`.
- **Consumer-first minor:** add an enum member. Enums are closed (an old
  consumer rejects an unknown member), so the rollout order is: release the
  contract, upgrade every consumer listed in the catalog, and only then
  let the producer emit the new member.
- **Major (breaking):** remove or rename a field, make an optional field
  required, add a required field, narrow a constraint, change a type,
  change meaning or units, change an ID derivation. Breaking changes need
  a new payload class with `SCHEMA_MAJOR = n+1` registered alongside the
  old one. The producer emits both during a migration window, until every
  consumer has moved.
- **Deprecation:** mark the field deprecated in its docstring and in the
  events doc. Producers keep filling it until the next major removes it.
- **Consumers ignore unknown fields** at every level (`extra="ignore"`),
  and accept any minor ≥ their own within the same major. Forward
  fixtures prove this.
- **Producers are strict:** models are frozen and `strict` (no silent
  coercion), and mypy runs the pydantic plugin with `init_forbid_extra`,
  so a misspelled field in producer code is a type error even though the
  runtime ignores unknown fields.
- **Golden fixtures are immutable** once released (`compat/fixtures`,
  SHA-256-pinned in `MANIFEST.json`); new behaviour gets new fixtures.

## Consequences

- Producers and consumers in both repositories use the same codec
  (`new_event`, `encode_event`, `decode_event`, `decode_event_as`).
- Unknown event types raise `UnknownEventTypeError`; a consumer that reads
  a shared stream logs and acknowledges them rather than crashing.
- P2 stores envelopes as JSON in the outbox and in stream entries (one
  field) and adds its own transport metadata alongside, never inside.
