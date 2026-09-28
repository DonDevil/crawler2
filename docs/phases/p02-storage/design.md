# P2 Design — storage layer

```
          antipiracy-contracts (P1)            models, IDs, envelope, catalog
                    │
     crawler2.storage.repositories             interfaces (Protocols) + read models
          │                     │
 crawler2.storage.scylla   crawler2.storage.objectstore
  (crawler2 keyspace)        (S3 API → MinIO, content-addressed)
          │
        outbox ──► storage.events.relay ──► Redis Streams ──► IdempotentConsumer
                                                              (processed_events)
```

Decisions: ADR-012 (Scylla model), ADR-013 (outbox/delivery), ADR-014
(object layout). Table mapping: [schema.md](schema.md).

## 1. What is authoritative, derived, operational

| Category | Tables / store | Rebuild | Retention (technical default) |
|---|---|---|---|
| Authoritative | fetch_attempts, page_observations(+_by_url), links_by_page_version, media_observations(+_by_media), evidence_by_match, evidence | — | kept (no TTL; ADR-005 open) |
| Authoritative windows | fetch_attempts_by_domain_day, fetch_attempts_by_url | from W1 | 30 days |
| Derived read models | latest_observation_by_url, page_versions_by_url, url_state, urls_by_domain, domains, observations_by_domain_day (30 d), inlinks_by_url, media, media_by_page_version, media_by_content, media_content_versions, evidence_by_target | `rebuild_url`, `rebuild_media`, replay; full scans in P13 | kept |
| Projections (fingerprinter facts) | representation_status, targets, matches_by_* | replay of fingerprinter events | kept |
| Operational | outbox, outbox_relay_checkpoints, processed_events | — | 14 days (outbox, markers) |
| Raw objects | `crawler2-raw` bucket | — | kept; deletion only reference-driven GC after ADR-005 |
| Evidence artifacts | `crawler2-evidence` (P12) | — | object lock per ADR-005 (P12) |

Legal retention is **unresolved** (ADR-005). Nothing authoritative expires;
every TTL above is on operational windows or rebuildable data and is set
per table in a migration, so P12 can extend or replace it.

## 2. Consistency

Named levels in `scylla/session.py` (identical at RF=1/RF=3):
authoritative + outbox writes `LOCAL_QUORUM`, derived writes `LOCAL_ONE`,
EC reads `LOCAL_ONE`, W7/E3/relay/schema reads `LOCAL_QUORUM`, E1/E2
`LOCAL_SERIAL`. At RF=1 every level is trivially satisfied; RF=3 behaviour
is designed but **not validated** (no multi-node environment; P14 entry
criterion).

## 3. Concurrency model

| Situation | Mechanism | Winner |
|---|---|---|
| same fact written twice / by two hosts | idempotent upsert keyed by contract IDs | identical rows |
| newer vs older observation of a URL | `USING TIMESTAMP latest_wins(observed_at)` | greatest observed_at |
| first sighting | `earliest_wins` | smallest instant |
| target versions | `version_wins(version)` | highest version; retirement terminal |
| representation ready vs failed | separate cells; read rule | ready > highest failed attempt > requested |
| one evidence item per match | LWT `IF NOT EXISTS` | first proposal |
| sealing | LWT `IF state = 'collecting'` | exactly one seal |
| schema migration | LWT lease row (TTL 300 s) | one migrator; others wait |

## 4. Schema management

`crawler2-storage migrate | status | check` (ADR-012; `migrations.py`).
Versioned, checksummed, additive-only CQL files packaged with the code;
the keyspace comes from configuration. Repositories call
`require_current()` and never alter schema. `validate-stack.sh` runs
`migrate` on both hosts at once to exercise the lock.

## 5. Configuration (additions to the one P0 schema)

`CRAWLER2_SCYLLA__REQUEST_TIMEOUT_S`; `CRAWLER2_MINIO__{BUCKET_RAW,
REQUEST_TIMEOUT_S, MAX_OBJECT_BYTES, SPOOL_MEMORY_BYTES}`;
`CRAWLER2_EVENTS__{STREAM_PREFIX, STREAM_MAXLEN, RELAY_BATCH_SIZE,
RELAY_SETTLE_S, RELAY_POLL_INTERVAL_S}`. Secrets stay `SecretStr`.

## 6. Observability

Only metrics with an operational use: `outbox_published_total{event_type}`,
`outbox_late_rows_total` (settle window too small → alert),
`outbox_publish_failures_total`, `consumer_events_total{consumer,outcome}`
(duplicate rate), `object_store_seconds{operation}`,
`object_store_errors_total{operation}`. Schema status is a CLI check
(`crawler2-storage check`). Per-statement repository latency is measured by
the benchmark rather than exported (it would add a histogram per call site
with no current consumer).

## 7. Known limitations

- RF=3 / multi-node consistency and failure behaviour: not validated.
- Repositories are synchronous (driver futures inside); async workers (P4)
  call them from a thread pool or get async variants additively.
- Pattern B events (links, evidence) rely on upstream redelivery to finish
  a crashed write; that upstream exists from P3/P5 on.
- Full-table rebuild and archival jobs: P13.
- Benchmark and latency limits of the dev host: [benchmarks.md](benchmarks.md).
