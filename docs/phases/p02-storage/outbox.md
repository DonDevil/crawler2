# P2 Outbox, publication and consumer idempotency

Rationale and guarantees: **ADR-013**. This page is the operating view.

## Flow

```
repository.record(entity, event=envelope)
   └─ logged batch: entity rows + outbox(shard=event_id%32, bucket=minute)   [pattern A]
      (links, evidence: rows first, outbox row last)                        [pattern B]
crawler2-storage relay            (one or more hosts)
   └─ read unpublished rows → XADD events:<type>.v<major> (exact stored bytes) → set published_at
consumer: RedisStreamReader (XREADGROUP/XACK/XAUTOCLAIM)
   └─ IdempotentConsumer.handle(bytes):
        decode_event_as (contracts) → idempotency_key (catalog paths)
        → processed? skip : handler(envelope) → mark processed
```

## What is guaranteed — and what is not

| Claim | Status |
|---|---|
| entity durable ⇒ its outbox row durable (pattern A) | yes (batchlog; eventually, not isolated) |
| outbox row durable ⇒ published at least once | yes, if a relay runs within 14 days |
| a relay crash loses events | no; it re-publishes (duplicates) |
| each event delivered exactly once | **no** — duplicates are part of the contract |
| each logical fact applied once by a consumer | per consumer, while its marker lives (14 d); re-application is still correct because repositories are idempotent |
| ordering | none (P1); consumers compare fact timestamps |

## Operating it

```bash
crawler2-storage relay                  # long-running; SIGTERM stops cleanly
crawler2-storage relay --once           # one cycle
crawler2-storage relay --sweep-hours 24 # republish rows found behind the checkpoint ("late")
crawler2-storage relay --replay-from 2026-09-28T00:00:00+00:00 [--replay-to ...]
```

Watch `outbox_late_rows_total` (non-zero ⇒ raise `relay_settle_s`) and
`outbox_publish_failures_total`. Stream length is capped approximately at
`stream_maxlen` (100k in dev): Redis is transport, the outbox is the
replay source.

## Consumer markers

`processed_events` `(consumer, idempotency_key)`, TTL 14 days. The key is
`["<event_type>", <major>, [values of the catalog paths]]`, e.g.
`["page.observed",1,["obs_…"]]`; a retried producer with a new event id
maps to the same key. Growth is bounded by TTL (one row per consumed
logical event per consumer for 14 days).

## Tests

- deterministic failure injection: `tests/unit/storage/test_events_unit.py`
  (crash after delivery before mark, checkpoint/settle, sweep, replay,
  consumer dedupe/retry) and `tests/integration/storage/test_outbox_objects.py`
  (same against Scylla + Redis, byte-identical publication, shard spread, TTL);
- real process kill + Scylla restart: `validate-stack.sh` → `multihost
  crash-*` (relay killed with `os._exit(137)` after 80 of 200 publications,
  `docker compose restart scylla`, recovery by host-2's relay).
